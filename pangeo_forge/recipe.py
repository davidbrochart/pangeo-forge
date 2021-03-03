"""
A Pangeo Forge Recipe
"""

import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Hashable, Iterable, NoReturn, Optional

import fsspec
import xarray as xr
import zarr
from rechunker.types import MultiStagePipeline, ParallelPipelines, Stage

from .patterns import ExplicitURLSequence, VariableSequencePattern
from .storage import AbstractTarget, UninitializedTarget
from .utils import chunked_iterable, fix_scalar_attr_encoding

logger = logging.getLogger(__name__)

# How to manually execute a recipe: ###
#
#   t = PangeoForgeTarget()
#   r = MyRecipe(target=t, **opts) # 1
#   # manual execution of recipe
#   for input_key in r.iter_inputs():
#       r.cache_input(input_key) # 4
#   r.prepare_target() # 3
#   for chunk_key in r.iter_chunks():
#       r.store_chunk(chunk_key) # 5
#   r.finalize_target() # 6


# 1) Initialize the Recipe object
# 2) Point the Recipe at its Target
# 3) Initialize the recipe.
#    Check if the target exists; if not, create it.
# 4) cache the inputs to proximate storage (OPTIONAL)
#    Some recipes won't need this (e.g. cloud to cloud)
#    If so, iter_inputs is just an empty iterator
# 5) Load each chunk from the inputs and store it in the target
#    Might be coming from the cache or might be read directly.
# 6)


@contextmanager
def input_opener(fname, **kwargs):
    logger.info(f"Opening input '{fname}'")
    with fsspec.open(fname, **kwargs) as f:
        yield f


class BaseRecipe(ABC):
    """Base recipe class from which all other Recipes inherit.
    """

    @property
    @abstractmethod
    def prepare_target(self) -> Callable[[], NoReturn]:
        """Prepare the recipe for execution by initializing the target.
        Attribute that returns a callable function.
        """
        pass

    @abstractmethod
    def iter_inputs(self) -> Iterable[Hashable]:
        """Iterate over all inputs."""
        pass

    @property
    @abstractmethod
    def cache_input(self) -> Callable[[Hashable], NoReturn]:
        """Copy an input from its source location to the cache.
        Attribute that returns a callable function.
        """
        pass

    @abstractmethod
    def iter_chunks(self) -> Iterable[Hashable]:
        """Iterate over all target chunks."""
        pass

    @property
    @abstractmethod
    def store_chunk(self) -> Callable[[Hashable], NoReturn]:
        """Store a chunk of data in the target.
        Attribute that returns a callable function.
        """
        pass

    @property
    @abstractmethod
    def finalize_target(self) -> Callable[[], NoReturn]:
        """Final step to finish the recipe after data has been written.
        Attribute that returns a callable function.
        """
        pass

    def to_pipelines(self) -> ParallelPipelines:
        """Translate recipe to pipeline for execution.
        """

        pipeline = []  # type: MultiStagePipeline
        pipeline.append(Stage(self.cache_input, list(self.iter_inputs())))
        pipeline.append(Stage(self.prepare_target))
        pipeline.append(Stage(self.store_chunk, list(self.iter_chunks())))
        pipeline.append(Stage(self.finalize_target))
        pipelines = []  # type: ParallelPipelines
        pipelines.append(pipeline)
        return pipelines

    # https://stackoverflow.com/questions/59986413/achieving-multiple-inheritance-using-python-dataclasses
    def __post_init__(self):
        # just intercept the __post_init__ calls so they
        # aren't relayed to `object`
        pass


# Notes about dataclasses:
# - https://www.python.org/dev/peps/pep-0557/#inheritance
# - https://stackoverflow.com/questions/51575931/class-inheritance-in-python-3-7-dataclasses


@dataclass
class NetCDFtoZarrRecipe(BaseRecipe):
    """This class represents a dataset composed of many individual NetCDF files.
    This class uses Xarray to read and write data and writes its output to Zarr.
    The files are assumed to be arranged in a sequence along dimension ``sequence_dim``
    (commonly "time".)

    :param sequence_dim: The dimension name along which the inputs will be concatenated.
    :param inputs_per_chunk: The number of inputs to use in each chunk.
    :param nitems_per_input: The length of each input along the `sequence_dim` dimension.
    :param target: A location in which to put the dataset. Can also be assigned at run time.
    :param input_cache: A location in which to cache temporary data.
    :param require_cache: Whether to allow opening inputs directly which have not
      yet been cached. This could lead to very slow behavior if the inputs
      live on a slow network.
    :param consolidate_zarr: Whether to consolidate the resulting Zarr dataset.
    :param xarray_open_kwargs: Extra options for opening the inputs with Xarray.
    :param xarray_concat_kwargs: Extra options to pass to Xarray when concatenating
      the inputs to form a chunk.
    :param delete_input_encoding: Whether to remove Xarray encoding from variables
      in the input dataset
    :param fsspec_open_kwargs: Extra options for opening the inputs with fsspec.
    :param process_input: Function to call on each opened input, with signature
      `(ds: xr.Dataset, filename: str) -> ds: xr.Dataset`.
    :param process_chunk: Function to call on each concatenated chunk, with signature
      `(ds: xr.Dataset) -> ds: xr.Dataset`.
    :param target_chunks: Desired chunk structure for the targret dataset.
    """

    sequence_dim: Optional[str] = None
    inputs_per_chunk: int = 1
    nitems_per_input: int = 1
    target: Optional[AbstractTarget] = field(default_factory=UninitializedTarget)
    input_cache: Optional[AbstractTarget] = field(default_factory=UninitializedTarget)
    require_cache: bool = True
    consolidate_zarr: bool = True
    xarray_open_kwargs: dict = field(default_factory=dict)
    xarray_concat_kwargs: dict = field(default_factory=dict)
    delete_input_encoding: bool = True
    fsspec_open_kwargs: dict = field(default_factory=dict)
    process_input: Optional[Callable[[xr.Dataset, str], xr.Dataset]] = None
    process_chunk: Optional[Callable[[xr.Dataset], xr.Dataset]] = None
    target_chunks: Optional[Dict[str, int]] = None

    def __post_init__(self):
        if self.target_chunks:
            sequence_dim_chunks = self.target_chunks.get(self.sequence_dim)
            if sequence_dim_chunks:
                if sequence_dim_chunks > self.nitems_per_input:
                    raise ValueError(
                        f"`sequence_dim_chunks` ({sequence_dim_chunks}) must be ",
                        f"<= `nitems_per_input` ({self.nitems_per_input})",
                    )
                if self.nitems_per_input % sequence_dim_chunks > 0:
                    raise ValueError(
                        "`sequence_dim_chunks` ({sequence_dim_chunks}) must "
                        f"evenly divide `nitems_per_input` ({self.nitems_per_input})"
                    )
        # TODO: more input validation

    @property
    def prepare_target(self) -> Callable:
        def _prepare_target():

            try:
                ds = self.open_target()
                logger.info("Found an existing dataset in target")
                logger.debug(f"{ds}")

                if self.target_chunks:
                    # TODO: check that target_chunks id compatibile with the
                    # existing chunks
                    pass

            except (FileNotFoundError, IOError, zarr.errors.GroupNotFoundError):

                init_dsets = []
                for chunk_key in self._init_chunks:
                    chunk_ds = self.open_chunk(
                        chunk_key
                    ).chunk()  # make sure data are not in memory
                    init_dsets.append(chunk_ds)
                # TODO: create customizable option for this step
                # How to combine attrs is particularly important. It seems like
                # xarray is missing a "minimal" option to only keep the attrs
                # that are the same among all input variables.
                ds = xr.merge(
                    init_dsets, compat="identical", join="exact", combine_attrs="override"
                )

                # make sure the concat dim has a valid fill_value to avoid
                # overruns when writing chunk
                # this is problematic because this value will always come out as NaN
                # when we open up the target
                # ds[self.sequence_dim].encoding = {"_FillValue": -1}
                # actually not necessary if we use decode_times=False

                if self.target_chunks:
                    # target_chunks has undergone some validation in __post_init__
                    ds = ds.chunk(self.target_chunks)

                self.initialize_target(ds)

            # Regardless of whether there is an existing dataset or we are creating a new one,
            # we need to expand the sequence_dim to hold the entire expected size of the data
            self.expand_target_dim(self.sequence_dim, self.sequence_len())

        return _prepare_target

    @property
    def cache_input(self) -> Callable:
        def cache_func(input_key: Hashable) -> None:
            if self._inputs[input_key]["processed"]:
                logger.info(f"Dry-run: caching input {input_key}")
            else:
                logger.info(f"Caching input {input_key}")
                fname = self._inputs[input_key]["url"]
                # TODO: add a check for whether the input is already cached?
                with input_opener(fname, mode="rb", **self.fsspec_open_kwargs) as source:
                    with self.input_cache.open(fname, mode="wb") as target:
                        # TODO: make this configurable? Would we ever want to change it?
                        BLOCK_SIZE = 10_000_000  # 10 MB
                        while True:
                            data = source.read(BLOCK_SIZE)
                            if not data:
                                break
                            target.write(data)

        return cache_func

    @property
    def store_chunk(self) -> Callable:
        def _store_chunk(chunk_key):
            write_region = self.region_for_chunk(chunk_key)
            if all(
                [
                    self._inputs[input_key]["processed"]
                    for input_key in self.inputs_for_chunk(chunk_key)
                ]
            ):
                logger.info(f"Dry-run: storing chunk '{chunk_key}' to Zarr region {write_region}")
            else:
                ds_chunk = self.open_chunk(chunk_key)

                def drop_vars(ds):
                    # writing a region means that all the variables MUST have sequence_dim
                    to_drop = [v for v in ds.variables if self.sequence_dim not in ds[v].dims]
                    return ds.drop_vars(to_drop)

                ds_chunk = drop_vars(ds_chunk)
                target_mapper = self.target.get_mapper()
                logger.info(f"Storing chunk '{chunk_key}' to Zarr region {write_region}")
                ds_chunk.to_zarr(target_mapper, region=write_region)

        return _store_chunk

    @property
    def finalize_target(self) -> Callable:
        def _finalize_target():
            if self.consolidate_zarr:
                logger.info("Consolidating Zarr metadata")
                target_mapper = self.target.get_mapper()
                zarr.consolidate_metadata(target_mapper)

        return _finalize_target

    @contextmanager
    def input_opener(self, fname: str):
        try:
            with self.input_cache.open(fname, mode="rb") as f:
                logger.info(f"Opening '{fname}' from cache")
                yield f
        except (IOError, FileNotFoundError):
            if self.require_cache:
                raise FileNotFoundError(
                    f"You are trying to open input {fname}, but the file is "
                    "not cached yet. First call `cache_input` or set "
                    "`require_cache=False`."
                )
            else:
                logger.info(f"No cache found. Opening input `{fname}` directly.")
                # This will bypass the cache. May be slow.
                with input_opener(fname, mode="rb", **self.file_system_kwargs) as f:
                    yield f

    def open_input(self, input_key: Hashable):
        fname = self._inputs[input_key]["url"]
        with self.input_opener(fname) as f:
            logger.info(f"Opening input with Xarray {input_key}: '{fname}'")
            ds = xr.open_dataset(f, **self.xarray_open_kwargs)
            # explicitly load into memory
            ds = ds.load()
        ds = fix_scalar_attr_encoding(ds)

        if self.delete_input_encoding:
            for var in ds.variables:
                ds[var].encoding = {}

        if self.process_input is not None:
            ds = self.process_input(ds, str(fname))

        logger.debug(f"{ds}")

        return ds

    def open_chunk(self, chunk_key):
        logger.info(f"Concatenating inputs for chunk '{chunk_key}'")
        inputs = self.inputs_for_chunk(chunk_key)
        dsets = [self.open_input(i) for i in inputs]
        # CONCAT DELETES ENCODING!!!
        # OR NO IT DOESN'T! Not in the latest version of xarray?
        ds = xr.concat(dsets, self.sequence_dim, **self.xarray_concat_kwargs)

        if self.process_chunk is not None:
            ds = self.process_chunk(ds)

        logger.debug(f"{ds}")

        # TODO: maybe do some chunking here?
        return ds

    def open_target(self):
        target_mapper = self.target.get_mapper()
        return xr.open_zarr(target_mapper)

    def initialize_target(self, ds, **expand_dims):
        logger.info("Creating a new dataset in target")
        target_mapper = self.target.get_mapper()
        ds.to_zarr(target_mapper, mode="w", compute=False)

    def expand_target_dim(self, dim, dimsize):
        target_mapper = self.target.get_mapper()
        zgroup = zarr.open_group(target_mapper)

        ds = self.open_target()
        sequence_axes = {v: ds[v].get_axis_num(dim) for v in ds.variables if dim in ds[v].dims}

        for v, axis in sequence_axes.items():
            arr = zgroup[v]
            shape = list(arr.shape)
            shape[axis] = dimsize
            arr.resize(shape)

        # now explicity write the sequence coordinate to avoid missing data
        # when reopening
        if dim in zgroup:
            zgroup[dim][self.nitems_per_input * self.processed_input_nb :] = 0  # noqa: E203

    def inputs_for_chunk(self, chunk_key):
        return self._chunks_inputs[chunk_key]

    def iter_inputs(self):
        for chunk_key in self.iter_chunks():
            for input in self.inputs_for_chunk(chunk_key):
                yield input

    def nitems_for_chunk(self, chunk_key):
        return self.nitems_per_input * len(self.inputs_for_chunk(chunk_key))

    def region_for_chunk(self, chunk_key):
        # return a dict suitable to pass to xr.to_zarr(region=...)
        # specifies where in the overall array to put this chunk's data
        stride = self.nitems_per_input * self.inputs_per_chunk
        start = self.chunk_position(chunk_key) * stride
        region_slice = slice(start, start + self.nitems_for_chunk(chunk_key))
        return {self.sequence_dim: region_slice}

    def sequence_chunks(self):
        # chunking
        return {self.sequence_dim: self.inputs_per_chunk * self.nitems_per_input}

    def iter_chunks(self):
        for k in self._chunks_inputs:
            yield k


@dataclass
class NetCDFtoZarrSequentialRecipe(NetCDFtoZarrRecipe):
    """There is only one sequence of input files. Each file can contain
    many variables.

    :param processed_input_urls: The inputs already used to generate the existing dataset.
    :param input_urls: The inputs used to generate the dataset.
    """

    processed_input_urls: Iterable[str] = field(repr=False, default_factory=list)
    input_urls: Iterable[str] = field(repr=False, default_factory=list)

    def __post_init__(self):
        super().__post_init__()
        self.processed_input_nb = len(self.processed_input_urls)
        input_pattern = ExplicitURLSequence(self.processed_input_urls + self.input_urls)
        self._inputs = {
            k: {"url": v, "processed": k < self.processed_input_nb} for k, v in input_pattern
        }
        self._chunks_inputs = {
            k: v for k, v in enumerate(chunked_iterable(self._inputs, self.inputs_per_chunk))
        }
        # just the first chunk is needed to initialize the recipe
        self._init_chunks = [next(iter(self._chunks_inputs))]

    def chunk_position(self, chunk_key):
        return chunk_key

    def sequence_len(self):
        # tells the total size of dataset along the sequence dimension
        return sum([self.nitems_for_chunk(k) for k in self.iter_chunks()])


@dataclass
class NetCDFtoZarrMultiVarSequentialRecipe(NetCDFtoZarrRecipe):
    """There are muliples sequences of input files (but all along the same dimension.)
    Different variables live in different files.

    :param input_pattern: An pattern used to generate the input file names.
    """

    input_pattern: VariableSequencePattern = field(default_factory=VariableSequencePattern)

    def __post_init__(self):
        super().__post_init__()
        self._variables = self.input_pattern.keys["variable"]
        self.processed_input_nb = 0  # TODO
        self._inputs = {k: {"url": v, "processed": False} for k, v in self.input_pattern}
        # input keys are tuples like
        #  ("temp", 0)
        #  ("temp", 1)
        #  ...
        #  ("salt", n)
        # chunk_keys will be similarly named, but the index will have a different meaning

        chunks_inputs = {}
        # generate this iteratively
        for vname in self._variables:
            # a hacky way to filter
            sequence_keys = [k[1] for k in self._inputs if k[0] == vname]
            for chunk_key, chunk_inputs in enumerate(
                chunked_iterable(sequence_keys, self.inputs_per_chunk)
            ):
                chunks_inputs[(vname, chunk_key)] = [
                    (vname, input_sequence_key) for input_sequence_key in chunk_inputs
                ]
        self._chunks_inputs = chunks_inputs

        # should be the first chunk from each variable
        self._init_chunks = [chunk_key for chunk_key in self._chunks_inputs if chunk_key[1] == 0]

    def chunk_position(self, chunk_key):
        return chunk_key[1]

    def sequence_len(self):
        n_sequence = len(self.input_pattern.keys[self.input_pattern._sequence_key])
        return self.nitems_per_input * n_sequence
