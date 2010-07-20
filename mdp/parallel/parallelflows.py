"""
Module for parallel flows that can handle the parallel training / execution.

Corresponding classes for task callables and ResultContainer are defined here 
as well.
"""

import mdp
from mdp import numx as n

from parallelnodes import TrainingPhaseNotParallelException
import scheduling
from mdp.hinet import FlowNode


### Train task classes ###

class FlowTaskCallable(scheduling.TaskCallable):
    """Base class for all flow callables.
    
    It deals activating the required extensions.
    """
    
    def __init__(self):
        """Store the currently active extensions."""
        self._used_extensions = mdp.get_active_extensions()
        super(FlowTaskCallable, self).__init__()
        
    def setup_environment(self):
        """Activate the used extensions."""
        # deactivate all active extensions for safety
        mdp.deactivate_extensions(mdp.get_active_extensions())
        mdp.activate_extensions(self._used_extensions)


class FlowTrainCallable(FlowTaskCallable):
    """Implements a single training phase in a flow for a data block.
    
    A FlowNode is used to simplify the forking process and to 
    encapsulate the flow.
    
    You can also derive from this class to define your own callable class. 
    """

    def __init__(self, flownode):
        """Store everything for the training.
        
        keyword arguments:
        flownode -- FlowNode containing the flow to be trained.
        """
        self._flownode = flownode
        super(FlowTrainCallable, self).__init__()
        
    def __call__(self, data):
        """Do the training and return only the trained node.
        
        data -- training data block (array or list if additional arguments are
            required)
        """
        if type(data) is n.ndarray:
            self._flownode.train(data)
        else:
            self._flownode.train(*data)
        # note the local training in ParallelFlow relies on the flownode
        # being preserved, so derived classes should preserve it as well
        for node in self._flownode:
            if node.is_training():
                return node
            
    def fork(self):
        return self.__class__(self._flownode.fork())
            

class NodeResultContainer(scheduling.ResultContainer):
    """Container for parallel nodes.
    
    Expects parallel nodes as results and joins them to save memory.
    A list containing one node is returned, so this container can replace
    the standard list container without any changes elsewhere.
    """
    
    def __init__(self):
        self._node = None
        
    def add_result(self, result, task_index):
        if not self._node:
            self._node = result
        else:
            self._node.join(result)
            
    def get_results(self):
        node = self._node
        self._node = None
        return [node,]
    

### Execute task classes ###

class FlowExecuteCallable(FlowTaskCallable):
    """Implements data execution through the whole flow.
    
    Note that one could also pass the flow itself as the callable, so this 
    class is not really needed. However, it serves as the base class for more
    complicated callables, e.g. which do some kind of preprocessing before
    executing the data with the flow.
    """

    def __init__(self, flow, nodenr=None):
        """Store everything for the execution.
        
        keyword arguments:
        flow -- flow instance for the execution
        nodenr -- optional nodenr argument for the flow execute method
        extensions -- List of the names of the extensions required by the
            callable. These are then activated by setup_environment.
        """
        self._flow = flow
        self._nodenr = nodenr
        super(FlowExecuteCallable, self).__init__()
        
    def __call__(self, x):
        """Return the execution result.
        
        x -- data chunk
        """
        return self._flow.execute(x, nodenr=self._nodenr)
    
    def fork(self):
        return self.__class__(self._flow, self._nodenr)
    

### ParallelFlow Class ###    

class ParallelFlowException(mdp.FlowException):
    """Standard exception for problems with ParallelFlow."""
    pass


class NoTaskException(ParallelFlowException):
    """Exception for problems with the task creation."""
    pass


class ParallelFlow(mdp.Flow):
    """A parallel flow provides the methods for parallel training / execution.
    
    Nodes in the flow which are not derived from ParallelNode are trained in 
    the normal way. The training is also done normally if fork() raises a 
    TrainingPhaseNotParallelException. This can be intentionally used by the 
    node to request local training without forking. 
    Parallel execution on the other hand should work for all nodes, since it 
    only relies on the copy method of nodes.
    The stop_training method is always called locally, with no forking or
    copying involved.
    
    Both parallel training and execution can be done conveniently by providing
    a scheduler instance to the train or execute method.
    It is also possible to manage the tasks manually. This is done via the
    methods setup_parallel_training (or execution), get_task and use_results.
    The code of the train / execute method can serve as an example how to use
    these methods and process the tasks by a scheduler.
    """
    
    def __init__(self, flow, verbose=False, **kwargs):
        """Initialize the internal variables.
        
        Note that the crash_recovery flag is is not supported, so it is
        disabled.
        """
        kwargs["crash_recovery"] = False
        super(ParallelFlow, self).__init__(flow, verbose=verbose,
                                           **kwargs)
        self._train_data_iterables = None  # all training data
        self._train_data_iterator = None  # iterator for current training
        # index of currently trained node, also used as flag for training 
        # takes value None for not training
        self._i_train_node = None
        self._flownode = None  # used during training
        # iterable for execution data 
        # also signals if parallel execution is underway
        self._exec_data_iterator = None  
        self._next_task = None  # buffer for next task
        self._train_callable_class = None
        self._execute_callable_class = None
    
    @mdp.with_extension("parallel")
    def train(self, data_iterables, scheduler=None, 
              train_callable_class=None,
              overwrite_result_container=True,
              **kwargs):
        """Train all trainable nodes in the flow.
        
        If a scheduler is provided the training will be done in parallel on the
        scheduler.
        
        data_iterables -- A list of iterables, one for each node in the flow.
            The iterators returned by the iterables must
            return data arrays that are then used for the node training.
            See Flow.train for more details.
            If a custom train_callable_class is used to preprocess the data
            then other data types can be used as well.
        scheduler -- Value can be either None for normal training (default 
            value) or a Scheduler instance for parallel training with the 
            scheduler.
            If the scheduler value is an iterable or iterator then it is
            assumed that it contains a scheduler for each training phase.
            After a node has been trained the scheduler is shutdown. Note that
            you can e.g. use a generator to create the schedulers just in time.
            For nodes which are not trained the scheduler can be None.
        train_callable_class -- Class used to create training callables for the
            scheduler. By specifying your own class you can implement data 
            transformations before the data is actually fed into the flow 
            (e.g. from 8 bit image to 64 bit double precision). 
            Note that the train_callable_class is only used if a scheduler was 
            provided. If a scheduler is provided the default class used is
            NodeResultContainer.
        overwrite_result_container -- If set to True (default value) then
            the result container in the scheduler will be overwritten with an
            instance of NodeResultContainer, if it is not already an instance
            of NodeResultContainer.
        """
        # Warning: If this method is updated you also have to update train
        #          in ParallelCheckpointFlow.
        if self.is_parallel_training:
            raise ParallelFlowException("Parallel training is underway.")
        if scheduler is None:
            if train_callable_class is not None:
                err = ("A train_callable_class was specified but no scheduler "
                       "was given, so the train_callable_class has no effect.")
                raise ParallelFlowException(err)
            super(ParallelFlow, self).train(data_iterables, **kwargs)
        else:
            if train_callable_class is None:
                train_callable_class = FlowTrainCallable
            schedulers = None
            # do parallel training
            try:
                self.setup_parallel_training(
                                    data_iterables, 
                                    train_callable_class=train_callable_class,
                                    **kwargs)
                # prepare scheduler
                if not isinstance(scheduler, scheduling.Scheduler):
                    # scheduler contains an iterable with the schedulers
                    # self._i_train_node was set in setup_parallel_training
                    schedulers = iter(scheduler)
                    scheduler = schedulers.next()
                    if self._i_train_node > 0:
                        # dispose schedulers for pretrained nodes
                        for _ in range(self._i_train_node):
                            if scheduler is not None:
                                scheduler.shutdown()
                            scheduler = schedulers.next()
                    elif self._i_train_node is None:
                        # all nodes are already trained, dispose schedulers
                        for _ in range(len(self.flow) - 1):
                            if scheduler is not None:
                                scheduler.shutdown()
                            # the last scheduler will be shutdown in finally
                            scheduler = schedulers.next()
                    last_trained_node = self._i_train_node
                else:
                    schedulers = None
                # check that the scheduler is compatible
                if ((scheduler is not None) and
                    overwrite_result_container and
                    (not isinstance(scheduler.result_container,
                                    NodeResultContainer))):
                    scheduler.result_container = NodeResultContainer()
                ## train all nodes
                while self.is_parallel_training:
                    while self.task_available:
                        task = self.get_task()
                        scheduler.add_task(*task)
                    results = scheduler.get_results()
                    if results == []:
                        err = ("Could not get any training tasks or results "
                               "for the current training phase.")
                        raise Exception(err)
                    else:
                        self.use_results(results)
                    # check if we have to switch to next scheduler
                    if ((schedulers is not None) and
                        (self._i_train_node is not None) and
                        (self._i_train_node > last_trained_node)):
                        # dispose unused schedulers
                        for _ in range(self._i_train_node - last_trained_node):
                            if scheduler is not None:
                                scheduler.shutdown()
                            scheduler = schedulers.next()
                        last_trained_node = self._i_train_node
                        # check that the scheduler is compatible
                        if ((scheduler is not None) and
                            overwrite_result_container and
                            (not isinstance(scheduler.result_container,
                                            NodeResultContainer))):
                            scheduler.result_container = NodeResultContainer()
            finally:
                # reset iterable references, which cannot be pickled
                self._train_data_iterables = None
                self._train_data_iterator = None
                if (schedulers is not None) and (scheduler is not None):
                    scheduler.shutdown()
    
    def setup_parallel_training(self, data_iterables, 
                                train_callable_class=FlowTrainCallable):
        """Prepare the flow for handing out tasks to do the training.
        
        After calling setup_parallel_training one has to pick up the
        tasks with get_task, run them and finally return the results via
        use_results. tasks are available as long as task_available returns 
        True. Training may require multiple phases, which are each closed by 
        calling use_results.
        
        data_iterables -- A list of iterables, one for each node in the flow.
            The iterators returned by the iterables must
            return data arrays that are then used for the node training.
            See Flow.train for more details.
            If a custom train_callable_class is used to preprocess the data
            then other data types can be used as well.
        train_callable_class -- Class used to create training callables for the
            scheduler. By specifying your own class you can implement data 
            transformations before the data is actually fed into the flow 
            (e.g. from 8 bit image to 64 bit double precision). 
        """
        if self.is_parallel_training:
            err = "Parallel training is already underway."
            raise ParallelFlowException(err)
        self._train_callable_class = train_callable_class
        self._train_data_iterables = self._train_check_iterables(data_iterables)
        self._i_train_node = 0
        self._next_train_phase()
            
    def _next_train_phase(self):
        """Find the next phase or node for parallel training.
        
        When it is found the corresponding internal variables are set.
        Nodes which are not derived from ParallelNode are trained locally. 
        If a fork() fails due to a TrainingPhaseNotParallelException
        in a certain train phase, then the training is done locally as well
        (but fork() is tested again for the next phase).
        """
        self._flownode = FlowNode(mdp.Flow(self.flow))
        # find next node that can be forked, if required do local training
        while self._i_train_node < len(self.flow):
            current_node = self.flow[self._i_train_node]
            if not current_node.is_training():
                self._i_train_node += 1
                continue
            data_iterable = self._train_data_iterables[self._i_train_node]
            try:
                self._flownode.fork()
                # fork successful, prepare parallel training
                if self.verbose:
                    print ("start parallel training phase of " +
                           "node no. %d in parallel flow" % 
                           (self._i_train_node+1))
                self._train_data_iterator = iter(data_iterable)
                first_task = self._create_train_task()
                # make sure that the iterator is not empty
                if first_task is None:
                    if current_node.get_current_train_phase() == 1:
                        err_str = ("The training data iteration for node "
                                   "no. %d could not be repeated for the "
                                   "second training phase, you probably "
                                   "provided an iterator instead of an "
                                   "iterable." % (self._i_train_node+1))
                        raise mdp.FlowException(err_str)
                    else:
                        err_str = ("The training data iterator for node "
                                   "no. %d is empty." % (self._i_train_node+1))
                        raise mdp.FlowException(err_str)
                task_data_chunk = first_task[0]
                # first task contains the new callable
                self._next_task = (task_data_chunk,
                            self._train_callable_class(self._flownode.fork()))
                break
            except TrainingPhaseNotParallelException, e:
                if self.verbose:
                    print ("could not fork node no. %d: %s" %
                           (self._i_train_node+1, str(e)))
                    print ("start nonparallel training phase of " + 
                           "node no. %d in parallel flow" % 
                           (self._i_train_node+1))
                self._local_train_phase(data_iterable)
                if self.verbose:
                    print ("finished nonparallel training phase of " + 
                           "node no. %d in parallel flow" % 
                           (self._i_train_node+1))
                self._stop_training_hook()
                self._flownode.stop_training()
                self._post_stop_training_hook()
                if not self.flow[self._i_train_node].is_training():
                    self._i_train_node += 1
        else:
            # training is finished
            self._i_train_node = None
            
    def _local_train_phase(self, data_iterable):
        """Perform a single training phase locally.
        
        The internal _train_callable_class is used for the training.
        """
        current_node = self.flow[self._i_train_node]
        task_callable = self._train_callable_class(self._flownode)
        empty_iterator = True   
        for i_task, data in enumerate(data_iterable):
            empty_iterator = False
            # Note: if x contains additional args assume that the
            # callable can handle this  
            task_callable(data)
            if self.verbose:
                print ("    finished nonparallel task no. %d" % (i_task+1))
        if empty_iterator:
            if current_node.get_current_train_phase() == 1:
                err_str = ("The training data iteration for node "
                           "no. %d could not be repeated for the "
                           "second training phase, you probably "
                           "provided an iterator instead of an "
                           "iterable." % (self._i_train_node+1))
                raise mdp.FlowException(err_str)
            else:
                err_str = ("The training data iterator for node "
                           "no. %d is empty." % (self._i_train_node+1))
                raise mdp.FlowException(err_str)
            
    def _post_stop_training_hook(self):
        """Hook method that is called after stop_training is called."""
        pass
            
    def _create_train_task(self):
        """Create and return a single training task without callable.
        
        Returns None if data iterator end is reached.
        """
        try:
            return (self._train_data_iterator.next(), None)
        except StopIteration:
            return None
            
    @mdp.with_extension("parallel")
    def execute(self, iterable, nodenr=None, scheduler=None, 
                execute_callable_class=None,
                overwrite_result_container=True):
        """Train all trainable nodes in the flow.
        
        If a scheduler is provided the execution will be done in parallel on
        the scheduler.
        
        iterable -- An iterable or iterator that returns data arrays that are
            used as input to the flow. Alternatively, one can specify one
            data array as input.
            If a custom execute_callable_class is used to preprocess the data
            then other data types can be used as well.
        nodenr -- Same as in normal flow, the flow is only executed up to the
            nodenr.
        scheduler -- Value can be either None for normal execution (default 
            value) or a Scheduler instance for parallel execution with the 
            scheduler.
        execute_callable_class -- Class used to create execution callables for 
            the scheduler. By specifying your own class you can implement data 
            transformations before the data is actually fed into the flow 
            (e.g. from 8 bit image to 64 bit double precision). 
            Note that the execute_callable_class is only used if a scheduler was 
            provided. If a scheduler is provided the default class used is
            NodeResultContainer.
        overwrite_result_container -- If set to True (default value) then
            the result container in the scheduler will be overwritten with an
            instance of OrderedResultContainer, if it is not already an 
            instance of OrderedResultContainer.
        """
        if self.is_parallel_training:
            raise ParallelFlowException("Parallel training is underway.")
        if scheduler is None:
            if execute_callable_class is not None:
                err = ("A execute_callable_class was specified but no "
                       "scheduler was given, so the execute_callable_class "
                       "has no effect.")
                raise ParallelFlowException(err)
            return super(ParallelFlow, self).execute(iterable, nodenr)
        if execute_callable_class is None:
            execute_callable_class = FlowExecuteCallable
        # check that the scheduler is compatible
        if overwrite_result_container:
            if not isinstance(scheduler.result_container,
                              scheduling.OrderedResultContainer):
                scheduler.result_container = scheduling.OrderedResultContainer()
        # do parallel training
        try:
            self.setup_parallel_execution(
                                iterable, 
                                nodenr=nodenr,
                                execute_callable_class=execute_callable_class)
            while self.task_available:
                task = self.get_task()
                scheduler.add_task(*task)
            result = self.use_results(scheduler.get_results())
        finally:
            # reset remaining iterator references, which cannot be pickled
            self._exec_data_iterator = None
        return result
       
    def setup_parallel_execution(self, iterable, nodenr=None,
                                 execute_callable_class=FlowExecuteCallable):
        """Prepare the flow for handing out tasks to do the execution.
        
        After calling setup_parallel_execution one has to pick up the
        tasks with get_task, run them and finally return the results via
        use_results. use_results will then return the result as if the flow was
        executed in the normal way.
        
        iterable -- An iterable or iterator that returns data arrays that are 
            used as input to the flow. Alternatively, one can specify one
            data array as input.
            If a custom execute_callable_class is used to preprocess the data
            then other data types can be used as well.
        nodenr -- Same as in normal flow, the flow is only executed up to the
            nodenr.
        execute_callable_class -- Class used to create execution callables for 
            the scheduler. By specifying your own class you can implement data 
            transformations before the data is actually fed into the flow 
            (e.g. from 8 bit image to 64 bit double precision). 
        """
        if self.is_parallel_training:
            raise ParallelFlowException("Parallel training is underway.")
        self._execute_callable_class = execute_callable_class
        if isinstance(iterable, n.ndarray):
            iterable = [iterable]
        self._exec_data_iterator = iter(iterable)
        first_task = self._create_execute_task()
        if first_task is None:
            errstr = ("The execute data iterator is empty.")
            raise mdp.FlowException(errstr)
        task_data_chunk = first_task[0]
        # first task contains the new callable
        self._next_task = (task_data_chunk,
                           self._execute_callable_class(mdp.Flow(self.flow)))
            
    def _create_execute_task(self):
        """Create and return a single execution task.
        
        Returns None if data iterator end is reached.
        """
        try:
            return (self._exec_data_iterator.next(), None)
        except StopIteration:
            return None
    
    def get_task(self):
        """Return a task either for either training or execution.
        
        A a one task buffer is used to make task_available work.
        tasks are available as long as need_result returns False or all the 
        training / execution is done. If no tasks are available a 
        NoTaskException is raised.
        """
        if self._next_task is not None:
            task = self._next_task
            if self._i_train_node is not None:
                self._next_task = self._create_train_task()
            elif self._exec_data_iterator is not None:
                self._next_task = self._create_execute_task()
            else:
                raise NoTaskException("No data available for execution task.")
            return task
        else:
            raise NoTaskException("No task available for execution.")
    
    @property
    def is_parallel_training(self):
        """Return True if parallel training is underway."""
        return self._i_train_node is not None
    
    @property
    def is_parallel_executing(self):
        """Return True if parallel execution is underway."""
        return self._exec_data_iterator is not None
    
    @property
    def task_available(self):
        """Return True if tasks are available, otherwise False.
        
        If False is returned this can indicate that results are needed to
        continue training.   
        """
        return self._next_task is not None
    
    def use_results(self, results):
        """Use the result from the scheduler.
        
        During parallel training this will start the next training phase.
        For parallel execution this will return the result, like a normal 
        execute would.
        
        results -- Iterable containing the results, normally the return value
            of scheduler.ResultContainer.get_results().  
            The individual results can be the return values of the tasks. 
        """
        if self.is_parallel_training:
            node = self.flow[self._i_train_node]
            for result in results:
                node.join(result)
            if self.verbose:
                print ("finished parallel training phase of node no. " + 
                       "%d in parallel flow" % (self._i_train_node+1))
            self._stop_training_hook()
            node.stop_training()
            self._post_stop_training_hook()
            if not node.is_training():
                self._i_train_node += 1
            self._next_train_phase()
        elif self.is_parallel_executing:
            self._exec_data_iterator = None
            return n.concatenate(results)
   
    
class ParallelCheckpointFlow(ParallelFlow, mdp.CheckpointFlow):
    """Parallel version of CheckpointFlow.
    
    Note that train phases are always closed, so e.g. CheckpointSaveFunction
    should not expect open train phases. This is necessary since otherwise
    stop_training() would be called remotely.
    """
    
    def __init__(self, flow, verbose=False, **kwargs):
        """Initialize the internal variables."""
        self._checkpoints = None
        super(ParallelCheckpointFlow, self).__init__(flow=flow,
                                                     verbose=verbose,
                                                     **kwargs)
    
    def train(self, data_iterables, checkpoints, scheduler=None, 
              train_callable_class=FlowTrainCallable,
              overwrite_result_container=True,
              **kwargs):
        """Train all trainable nodes in the flow.
        
        Same as the train method in ParallelFlow, but with additional support
        of checkpoint functions as in CheckpointFlow.
        """
        super(ParallelCheckpointFlow, self).train(
                        data_iterables=data_iterables, 
                        scheduler=scheduler, 
                        train_callable_class=train_callable_class,
                        overwrite_result_container=overwrite_result_container,
                        checkpoints=checkpoints,
                        **kwargs)
    
    def setup_parallel_training(self, data_iterables, checkpoints, 
                                train_callable_class=FlowTrainCallable,
                                **kwargs):
        """Checkpoint version of parallel training."""
        self._checkpoints = self._train_check_checkpoints(checkpoints)
        super(ParallelCheckpointFlow, self).setup_parallel_training(
                                    data_iterables, 
                                    train_callable_class=train_callable_class,
                                    **kwargs)
    
    def _post_stop_training_hook(self):
        """Check if we reached a checkpoint."""
        super(ParallelCheckpointFlow, self)._post_stop_training_hook()
        i_node = self._i_train_node
        if self.flow[i_node].get_remaining_train_phase() == 0:
            if ((i_node <= len(self._checkpoints)) 
                and self._checkpoints[i_node]):
                dict = self._checkpoints[i_node](self.flow[i_node])
                # store result, just like in the original CheckpointFlow
                if dict: 
                    self.__dict__.update(dict)
