from builtins import str
from builtins import object
from copy import copy, deepcopy
from logging import getLogger
import numpy as np
from nipype.pipeline import engine as pe
from nipype.interfaces.utility import IdentityInterface
from arcana.requirement import RequirementManager
from arcana.exception import (
    ArcanaError, ArcanaMissingDataException,
    ArcanaNoRunRequiredException, ArcanaUsageError)
from arcana.data import BaseFileset
from arcana.utils import PATH_SUFFIX, FIELD_SUFFIX


logger = getLogger('arcana')


WORKFLOW_MAX_NAME_LEN = 100


class BaseProcessor(object):
    """
    A thin wrapper around the NiPype LinearPlugin used to connect
    runs pipelines on the local workstation

    Parameters
    ----------
    work_dir : str
        A directory in which to run the nipype workflows
    max_process_time : float
        The maximum time allowed for the process
    reprocess: True|False|'all'
        A flag which determines whether to rerun the processing for this
        step. If set to 'all' then pre-requisite pipelines will also be
        reprocessed.
    """

    default_plugin_args = {}

    def __init__(self, work_dir, requirement_manager=None,
                 max_process_time=None, reprocess=False, **kwargs):
        self._work_dir = work_dir
        self._max_process_time = max_process_time
        self._reprocess = reprocess
        self._plugin_args = copy(self.default_plugin_args)
        self._plugin_args.update(kwargs)
        self._init_plugin()
        self._study = None
        self._requirement_manager = (
            requirement_manager if requirement_manager is not None
            else RequirementManager())

    def __repr__(self):
        return "{}(work_dir={})".format(
            type(self).__name__, self._work_dir)

    def __eq__(self, other):
        try:
            return (
                self._work_dir == other._work_dir and
                self._max_process_time == other._max_process_time and
                self._requirement_manager == other._requirement_manager and
                self._reprocess == other._reprocess and
                self._plugin_args == other._plugin_args)
        except AttributeError:
            return False

    def _init_plugin(self):
        self._plugin = self.nipype_plugin_cls(**self._plugin_args)

    @property
    def study(self):
        return self._study

    @property
    def requirement_manager(self):
        return self._requirement_manager

    def requirements_satisfiable(self, *requirements, **kwargs):
        self.requirement_manager.satisfiable(*requirements, **kwargs)

    def load_requirements(self, *requirements, **kwargs):
        self.requirement_manager.load(*requirements, **kwargs)

    def unload_requirements(self, *requirements, **kwargs):
        self.requirement_manager.unload(*requirements, **kwargs)

    def bind(self, study):
        cpy = deepcopy(self)
        cpy._study = study
        return cpy

    def run(self, *pipelines, **kwargs):
        """
        Connects all pipelines to that study's repository and runs them
        in the same NiPype workflow

        Parameters
        ----------
        pipeline(s) : Pipeline, ...
            The pipeline to connect to repository
        subject_ids : list[str]
            The subset of subject IDs to process. If None all available will be
            processed. Note this is not a duplication of the study
            and visit IDs passed to the Study __init__, as they define the
            scope of the analysis and these simply limit the scope of the
            current run (e.g. to break the analysis into smaller chunks and
            run separately). Therefore, if the analysis joins over subjects,
            then all subjects will be processed and this parameter will be
            ignored.
        visit_ids : list[str]
            The same as 'subject_ids' but for visit IDs
        session_ids : list[str,str]
            The same as 'subject_ids' and 'visit_ids', except specifies a set
            of specific combinations in tuples of (subject ID, visit ID).
        force : bool | 'all'
            A flag to force the reprocessing of all sessions in the filter
            array, regardless of whether the parameters|pipeline used
            to generate them matches the current ones. NB: if True only the
            final pipeline will be reprocessed (prerequisite pipelines won't
            run unless they don't match provenance). To process all
            prerequisite pipelines 'all' should be passed to force.

        Returns
        -------
        report : ReportNode
            The final report node, which can be connected to subsequent
            pipelines
        """
        if not pipelines:
            raise ArcanaUsageError(
                "No pipelines provided to {}.run"
                .format(self))
        # Get filter kwargs  (NB: in Python 3 they could be in the arg list)
        subject_ids = kwargs.pop('subject_ids', None)
        visit_ids = kwargs.pop('visit_ids', None)
        session_ids = kwargs.pop('session_ids', None)
        # Create name by combining pipelines
        name = '_'.join(p.name for p in pipelines)
        # Trim the end of very large names to avoid problems with
        # work-dir paths exceeding system limits.
        name = name[:WORKFLOW_MAX_NAME_LEN]
        workflow = pe.Workflow(name=name, base_dir=self.work_dir)
        already_connected = {}
        # Generate filter array to optionally restrict the run to certain
        # subject and visit IDs.
        tree = self.study.tree
        # Create maps from the subject|visit IDs to an index used to represent
        # them in the filter array
        subject_inds = {s.id: i for i, s in enumerate(tree.subjects)}
        visit_inds = {v.id: i for i, v in enumerate(tree.visits)}
        if subject_ids is None and visit_ids is None and session_ids is None:
            # No filters applied so create a full filter array
            filter_array = np.ones((len(subject_inds), len(visit_inds)),
                                   dtype=bool)
        else:
            # Filters applied so create an empty filter array and populate
            # from filter lists
            filter_array = np.zeros((len(subject_inds), len(visit_inds)),
                                    dtype=bool)
            for subj_id in subject_ids:
                filter_array[subject_inds[subj_id], :] = True
            for visit_id in visit_ids:
                filter_array[:, visit_inds[visit_id]] = True
            for subj_id, visit_id in session_ids:
                filter_array[subject_inds[subj_id],
                             visit_inds[visit_id]] = True
            if not filter_array.any():
                raise ArcanaUsageError(
                    "Provided filters:\n" +
                    ("  subject_ids: {}\n".format(', '.join(subject_ids))
                     if subject_ids is not None else '') +
                    ("  visit_ids: {}\n".format(', '.join(visit_ids))
                     if visit_ids is not None else '') +
                    ("  session_ids: {}\n".format(', '.join(session_ids))
                     if session_ids is not None else '') +
                    "Did not match any sessions in the project:\n" +
                    "  subject_ids: {}\n".format(', '.join(subject_inds)) +
                    "  visit_ids: {}\n".format(', '.join(visit_inds)))
        for pipeline in pipelines:
            try:
                self._connect_pipeline(deepcopy(pipeline), workflow,
                                       subject_inds, visit_inds, filter_array,
                                       already_connected=already_connected,
                                       **kwargs)
            except ArcanaNoRunRequiredException:
                logger.info("Not running '{}' pipeline as its outputs "
                            "are already present in the repository"
                            .format(pipeline.name))
        # Reset the cached tree of filesets in the repository as it will
        # change after the pipeline has run.
        self.study.repository.clear_cache()
        return workflow.run(plugin=self._plugin)

    def _connect_pipeline(self, pipeline, workflow, subject_inds, visit_inds,
                          filter_array, already_connected=None, force=False):
        """
        Connects a pipeline to a overarching workflow that sets up iterators
        over subjects|visits present in the repository (if required) and
        repository source and sink nodes

        Parameters
        ----------
        pipeline : Pipeline
            The pipeline to connect
        workflow : nipype.pipeline.engine.Workflow
            The overarching workflow to connect the pipeline to
        subject_inds : dct[str, int]
            A mapping of subject ID to row index in the filter array
        visit_inds : dct[str, int]
            A mapping of visit ID to column index in the filter array
        filter_array : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows correspond to
            subjects and columns correspond to visits in the repository. True
            values represent a combination of subject & visit ID to include
            in the current round of processing. Note that if the 'force'
            flag is not set, sessions won't be reprocessed unless the
            save provenance doesn't match that of the given pipeline.
        already_connected : dict[str, Pipeline]
            A dictionary containing all pipelines that have already been
            connected to avoid the same pipeline being connected twice.
        force : bool
            A flag to force the processing of all sessions in the filter
            array, regardless of whether the parameters|pipeline used
            to generate existing data matches the given pipeline
        """
        if already_connected is None:
            already_connected = {}
        try:
            (prev_connected, final) = already_connected[pipeline.name]
            if prev_connected == pipeline:
                return final
            else:
                raise ArcanaError(
                    "Name clash between {} and {} non-matching "
                    "prerequisite pipelines".format(prev_connected,
                                                    pipeline))
        except KeyError:
            pass  # Continue to connect pipeline to repository
        # Get list of sessions that need to be processed (i.e. if
        # they don't contain the outputs of this pipeline)
        to_process = self._to_process(
            pipeline, filter_array, subject_inds, visit_inds, force=force)
        # Set up workflow to run the pipeline, loading and saving from the
        # repository
        workflow.add_nodes([pipeline._workflow])
        # Prepend prerequisite pipelines to complete workflow if required
        prereq_finals = {}
        for prereq in pipeline.prerequisites:
            # NB: Even if reprocess==True, the prerequisite pipelines
            # are not re-processed, they are only reprocessed if
            # reprocess == 'all'
            try:
                prereq_finals[prereq.name] = self._connect_pipeline(
                    prereq, workflow, subject_inds, visit_inds,
                    filter_array=to_process,
                    already_connected=already_connected,
                    force=(force if force == 'all' else False))
            except ArcanaNoRunRequiredException:
                logger.info(
                    "Not running '{}' pipeline as a "
                    "prerequisite of '{}' as the required "
                    "outputs are already present in the repository"
                    .format(prereq.name, pipeline.name))
        # If prerequisite pipelines need to be processed, connect their
        # "final" nodes to the initial node of this pipeline to ensure that
        # they are all processed before this pipeline is run.
        initial = pipeline.add(
            'initial', IdentityInterface([n + '_link' for n in prereq_finals] +
                                         ['link']))
        for name, final in prereq_finals:
            workflow.connect(final, 'link', initial, name + '_link')
        # Construct iterator structure over subjects and sessions to be
        # processed
        iterators = self._iterate(pipeline, to_process, subject_inds,
                                  visit_inds)
        # Loop through each frequency present in the pipeline inputs and
        # create a corresponding source node
        for freq in pipeline.input_frequencies:
            try:
                # Create source and sinks from the repository
                source = self.study.source(
                    pipeline.inputs,
                    name='{}_{}_source'.format(pipeline.name, freq))
            except ArcanaMissingDataException as e:
                raise ArcanaMissingDataException(
                    str(e) + ", which is required for pipeline '{}'".format(
                        pipeline.name))
            inputs = pipeline.frequency_inputs(freq)
            inputnode = pipeline.inputnode(freq)
            source = self.study.source(
                [o.name for o in inputs], frequency=freq,
                name='{}_{}_source'.format(pipeline.name, freq))
            # Connect source node to initial node of pipeline to ensure
            # they are run after any prerequisites
            workflow.connect(initial, 'link', source, 'link')
            # Connect iterators to source and input nodes
            for iterfield in pipeline.iterfields(freq):
                workflow.connect(iterators[freq], iterfield, source, iterfield)
                workflow.connect(iterators[freq], iterfield, inputnode,
                                 iterfield)
            for input in inputs:  # @ReservedAssignment
                in_name = input.name + (
                    PATH_SUFFIX if isinstance(input, BaseFileset) else
                    FIELD_SUFFIX)
                workflow.connect(source, in_name, inputnode, input.name)
        deiterators = {}
        # Connect all outputs to the repository sink, creating a new sink for
        # each frequency level (i.e 'per_session', 'per_subject', 'per_visit',
        # or 'per_study')
        for freq in pipeline.output_frequencies:
            outputs = pipeline.frequency_outputs(freq)
            outputnode = pipeline.outputnode(freq)
            sink = self.study.sink(
                [o.name for o in outputs], frequency=freq,
                name='{}_{}_sink'.format(pipeline.name, freq))
            for iterfield in pipeline.iterfields:
                workflow.connect(iterators[freq], iterfield, sink, iterfield)
            for output in outputs:
                if output.is_spec:  # Skip outputs that are study inputs
                    out_name = output.name + (
                        PATH_SUFFIX if isinstance(output, BaseFileset) else
                        FIELD_SUFFIX)
                    workflow.connect(outputnode, output.name, sink, out_name)
            # Join over iterated fields to get back to single child node
            # by the time we connect to the final node of the pipeline

            # Set the sink and subject_id as the default deiterator if there
            # are no deiterates (i.e. per_study) or to use as the upstream
            # node to connect the first deiterator for every frequency
            deiterators[freq] = sink, pipeline.SUBJECT_ITERFIELD
            if list(pipeline.iterfields(freq)):
                for iterfield in pipeline.iterfields(freq):
                    joinsource = ('subjects'
                                  if iterfield == pipeline.SUBJECT_ITERFIELD
                                  else 'visits')
                    deiterator = pipeline.add(
                        '{}_{}_deiterator'.foramt(freq, iterfield),
                        IdentityInterface(list(pipeline.iterfields(freq))),
                        joinsource=joinsource, joinfield=iterfield)
                    # Connect to previous deiterator or sink
                    upstream, _ = deiterators[freq]
                    pipeline.connect(upstream, iterfield, deiterator,
                                     iterfield)
                    deiterators[freq] = deiterator, iterfield
        # Create a final node, which is used to connect with dependent
        # pipelines into large workflows
        final = pipeline.add(
            'final', IdentityInterface(fields=list(deiterators) + ['link']))
        # The name of the pipeline is used to connect with downstream
        # pipelines (i.e. ones that this pipeline is a prerequisite)
        final.inputs.name = pipeline.name
        for freq, (deiterator, iterfield) in deiterators.items():
            # Connect the output summary of the prerequisite to the
            # pipeline to ensure that the prerequisite is run first.
            workflow.connect(deiterator, iterfield, final, freq)
        # Register pipeline as being connected to prevent duplicates
        already_connected[pipeline.name] = (pipeline, final)
        return final

    def _iterate(self, pipeline, to_process, subject_inds, visit_inds):
        """
        Generate nodes that iterate over subjects and visits in the study that
        need to be processed by the pipeline

        Parameters
        ----------
        pipeline : Pipeline
            The pipeline to add iterators for
        to_process : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows correspond to
            subjects and columns correspond to visits in the repository. True
            values represent a combination of subject & visit ID to process
            the session for
        subject_inds : dct[str, int]
            A mapping of subject ID to row index in the 'to_process' array
        visit_inds : dct[str, int]
            A mapping of visit ID to column index in the 'to_process' array

        Returns
        -------
        iterators : dict[str, Node]
            A dictionary containing the iterators required for the pipeline
            process all sessions that need processing.
        """
        # Check to see whether the subject/visit IDs to process (as specified
        # by the 'to_process' array) can be factorized into indepdent nodes,
        # i.e. all subjects to process have the same visits to process and
        # vice-versa.
        factorizable = True
        if len(list(pipeline.iterfields)) == 2:
            nz_rows = to_process[to_process.any(axis=0), :]
            ref_row = nz_rows[0, :]
            factorizable = all((r == ref_row).all() for r in nz_rows)
        # If the subject/visit IDs to process cannot be factorized into
        # indepdent iterators, determine which to make make dependent on the
        # other in order to avoid/minimise duplicatation of download attempts
        dependent = None
        if not factorizable:
            input_freqs = list(pipeline.input_frequencies)
            if 'per_visit' in input_freqs:
                if 'per_subject' in input_freqs:
                    # If both per_visit and per_subject inputs are used by
                    # the pipeline then pick the one with the most IDs to
                    # iterate to be the dependent to reduce the number of
                    # duplication of download attempts across the nodes
                    (num_subjs,
                     num_visits) = nz_rows[:, nz_rows.any(axis=1)].shape
                    if num_subjs > num_visits:
                        dependent = 'subjects'
                    else:
                        dependent = 'visits'
                    logger.warning(
                        "Cannot factorize sessions to process into independent"
                        " subject and visit iterators and both 'per_visit' and"
                        " 'per_subject' inputs are used by pipeline therefore"
                        " per_{} inputs may be cached twice".format(
                            dependent[:-1]))
                else:
                    dependent = 'subjects'
            else:
                assert 'per_subject' in input_freqs
                dependent = 'visits'
        # Invert the index dictionaries to get index-to-ID maps
        subj_ids = {v: k for k, v in subject_inds.items()}
        visit_ids = {v: k for k, v in visit_inds.items()}
        # Create iterator for subjects
        iterators = {}
        if pipeline.SUBJECT_ITERFIELD in pipeline.iterfields:
            fields = [pipeline.SUBJECT_ITERFIELD]
            if dependent == 'subjects':
                fields += pipeline.VISIT_ITERFIELD
            subj_it = pipeline.add('subjects', IdentityInterface(fields))
            if dependent == 'subjects':
                # Subjects iterator is dependent on visit iterator (because of
                # non-factorizable IDs)
                subj_it.itersource = ('visits', pipeline.VISIT_ITERFIELD)
                subj_it.iterables = [(
                    pipeline.SUBJECT_ITERFIELD,
                    {visit_ids[n]: [subj_ids[m] for m in col.nonzero()[0]]
                     for n, col in enumerate(to_process.T)})]
                pipeline.connect('visits', pipeline.VISIT_ITERFIELD,
                                 'subjects', pipeline.VISIT_ITERFIELD)
            else:
                subj_it.iterables = (
                    pipeline.SUBJECT_ITERFIELD,
                    [subj_ids[n] for n in to_process.any(axis=0).nonzero()[0]])
            iterators['subject'] = subj_it
        # Create iterator for visits
        if pipeline.VISIT_ITERFIELD in pipeline.iterfields:
            fields = [pipeline.VISIT_ITERFIELD]
            if dependent == 'visits':
                fields += pipeline.SUBJECT_ITERFIELD
            visit_it = pipeline.add('visits', IdentityInterface(fields))
            if dependent == 'visits':
                visit_it.itersource = ('subjects', pipeline.SUBJECT_ITERFIELD)
                visit_it.iterables = [(
                    pipeline.VISIT_ITERFIELD,
                    {subj_ids[m]: [visit_ids[n] for n in row.nonzero()[0]]
                     for n, row in enumerate(to_process)})]
                pipeline.connect('subjects', pipeline.VISIT_ITERFIELD,
                                 'visits', pipeline.VISIT_ITERFIELD)
            else:
                visit_it.iterables = (
                    pipeline.VISIT_ITERFIELD,
                    [visit_ids[n]
                     for n in to_process.any(axis=1).nonzero()[0]])
        return iterators

    def _to_process(self, pipeline, filter_array, subject_inds, visit_inds,
                    force=False):
        """
        Check whether the outputs of the pipeline are present in all sessions
        in the project repository and were generated with matching parameters
        and pipelines. Return an 2D boolean array (subjects: rows,
        visits: cols) with the sessions to process marked True.

        Parameters
        ----------
        pipeline : Pipeline
            The pipeline to determine the sessions to process
        filter_array : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows and columns correspond
            correspond to subjects and visits in the repository tree. True
            values represent a subject/visit ID pairs to include
            in the current round of processing. Note that if the 'force'
            flag is not set, sessions won't be reprocessed unless the
            parameters and pipeline version saved in the provenance doesn't
            match that of the given pipeline.
        subject_inds : dict[str,int]
            Mapping from subject ID to index in filter|to_process arrays
        visit_inds : dict[str,int]
            Mapping from visit ID to index in filter|to_process arrays
        force : bool
            Whether to force reprocessing of all (filtered) sessions or not

        Returns
        -------
        to_process : 2-D numpy.array[bool]
            A two-dimensional boolean array, where rows correspond to
            subjects and columns correspond to visits in the repository. True
            values represent subject/visit ID pairs to run the pipeline for
        """
        # Check to see if the pipeline has any low frequency outputs, because
        # if not then each session can be processed indepdently. Otherwise,
        # the "session matrix" (as defined by subject_ids and visit_ids
        # passed to the Study class) needs to be complete, i.e. a session
        # exists (with the full complement of requird inputs) for each
        # subject/visit ID pair.
        tree = self.study.tree
        low_freq_outputs = [
            o.name for o in pipeline.outputs if o.frequency != 'per_session']
        if low_freq_outputs and list(tree.incomplete_subjects):
            raise ArcanaUsageError(
                "Can't process '{}' pipeline as it has low frequency outputs "
                "(i.e. outputs that aren't of 'per_session' frequency) "
                "({}) and subjects ({}) that are missing one "
                "or more visits ({}). Please restrict the subject/visit "
                "IDs in the study __init__ to continue the analysis"
                .format(
                    self.name,
                    ', '.join(low_freq_outputs),
                    ', '.join(s.id for s in tree.incomplete_subjects),
                    ', '.join(v.id for v in tree.incomplete_visits)))
        # Initialise an array of sessions to process
        to_process = np.zeros((len(subject_inds), len(visit_inds)), dtype=bool)
        for output in pipeline.frequency_outputs('per_study'):
            collection = self.study.spec(output).collection
            # Include all sessions if a per-study output needs to be
            # reprocessed. Note that this will almost always be the case if
            # any other output needs to be reprocessed.
            #
            # NB: Filter array should always have at least one true value at
            # this point
            if pipeline.to_process(collection.item(), force=force):
                to_process[:] = True
        for output in pipeline.frequency_outputs('per_subject'):
            collection = self.study.spec(output).collection
            for item in collection:
                i = subject_inds[item.subject_id]
                # NB: The output will be reprocessed using data from every
                # visit of each subject. However, the visits to include in the
                # analysis can be specified the initialisation of the Study.
                if pipeline.to_process(item, force) and filter_array[i,
                                                                     :].any():
                    to_process[i, :] = True
        for output in pipeline.frequency_outputs('per_visit'):
            collection = self.study.spec(output).collection
            for item in collection:
                j = visit_inds[item.visit_id]
                # NB: The output will be reprocessed using data from every
                # subject of each vist. However, the subject to include in the
                # analysis can be specified the initialisation of the Study.
                if pipeline.to_process(item, force) and filter_array[:,
                                                                     j].any():
                    to_process[:, j] = True
        for output in pipeline.frequency_outputs('per_session'):
            collection = self.study.spec(output).collection
            for item in collection:
                i = subject_inds[item.subject_id]
                j = visit_inds[item.visit_id]
                if pipeline.to_process(item, force) and filter_array[i, j]:
                    to_process[i, j] = True
        if not to_process.any():
            raise ArcanaNoRunRequiredException(
                "No sessions to process for '{}' pipeline"
                .format(pipeline.name))
        return to_process

    @property
    def work_dir(self):
        return self._work_dir

    def __getstate__(self):
        dct = copy(self.__dict__)
        # Delete the NiPype plugin as it can be regenerated
        del dct['_plugin']
        return dct

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._init_plugin()
