from contextlib import nullcontext as does_not_raise

import aiohttp
import pytest
import xarray as xr

from pangeo_forge import recipe
from pangeo_forge.patterns import VariableSequencePattern
from pangeo_forge.storage import UninitializedTargetError

dummy_fnames = ["a.nc", "b.nc", "c.nc"]


def incr_date(ds, filename=""):
    # add one day
    t = [d + int(24 * 3600e9) for d in ds.time.values]
    ds = ds.assign_coords(time=t)
    return ds


def _manually_execute_recipe(r):
    for input_key in r.iter_inputs():
        r.cache_input(input_key)
    r.prepare_target()
    for chunk_key in r.iter_chunks():
        r.store_chunk(chunk_key)
    r.finalize_target()


def test_NetCDFtoZarrSequentialRecipeIncremental(
    daily_xarray_dataset, netcdf_local_paths, tmp_target, tmp_cache
):

    paths, items_per_file = netcdf_local_paths
    n = len(paths) // 2

    paths1 = paths[:n]
    r = recipe.NetCDFtoZarrSequentialRecipe(
        input_urls=paths1,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=items_per_file,
        target=tmp_target,
        input_cache=tmp_cache,
    )
    _manually_execute_recipe(r)

    paths2 = paths[n:]
    r = recipe.NetCDFtoZarrSequentialRecipe(
        processed_input_urls=paths1,
        input_urls=paths2,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=items_per_file,
        target=tmp_target,
        input_cache=tmp_cache,
    )
    _manually_execute_recipe(r)

    ds_target = xr.open_zarr(tmp_target.get_mapper(), consolidated=True).load()
    ds_expected = daily_xarray_dataset.compute()
    assert ds_target.identical(ds_expected)


@pytest.mark.parametrize(
    "username, password", [("foo", "bar"), ("foo", "wrong"),],  # noqa: E231
)
def test_NetCDFtoZarrSequentialRecipeHttpAuth(
    daily_xarray_dataset, netcdf_http_server, tmp_target, tmp_cache, username, password
):

    url, fnames, items_per_file = netcdf_http_server("foo", "bar")
    urls = [f"{url}/{fname}" for fname in fnames]
    r = recipe.NetCDFtoZarrSequentialRecipe(
        input_urls=urls,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=items_per_file,
        target=tmp_target,
        input_cache=tmp_cache,
        fsspec_open_kwargs={"client_kwargs": {"auth": aiohttp.BasicAuth(username, password)}},
    )

    if password == "wrong":
        with pytest.raises(aiohttp.client_exceptions.ClientResponseError):
            r.cache_input(next(r.iter_inputs()))
    else:
        _manually_execute_recipe(r)

        ds_target = xr.open_zarr(tmp_target.get_mapper(), consolidated=True).load()
        ds_expected = daily_xarray_dataset.compute()
        assert ds_target.identical(ds_expected)


@pytest.mark.parametrize(
    "process_input, process_chunk",
    [(None, None), (incr_date, None), (None, incr_date), (incr_date, incr_date)],
)
@pytest.mark.parametrize("inputs_per_chunk", [1, 2])
@pytest.mark.parametrize(
    "target_chunks,chunk_expectation",
    [
        (None, does_not_raise()),
        ({"lon": 12}, does_not_raise()),
        ({"lon": 12, "time": 1}, does_not_raise()),
        ({"lon": 12, "time": 3}, pytest.raises(ValueError)),
    ],
)
def test_NetCDFtoZarrSequentialRecipe(
    daily_xarray_dataset,
    netcdf_local_paths,
    tmp_target,
    tmp_cache,
    process_input,
    process_chunk,
    inputs_per_chunk,
    target_chunks,
    chunk_expectation,
):

    # the same recipe is created as a fixture in conftest.py
    # I left it here explicitly because it makes the test easier to read.
    paths, items_per_file = netcdf_local_paths
    with chunk_expectation as excinfo:
        r = recipe.NetCDFtoZarrSequentialRecipe(
            input_urls=paths,
            sequence_dim="time",
            inputs_per_chunk=inputs_per_chunk,
            nitems_per_input=items_per_file,
            target=tmp_target,
            input_cache=tmp_cache,
            process_input=process_input,
            process_chunk=process_chunk,
            target_chunks=target_chunks,
        )
    if excinfo:
        # don't continue if we got an exception
        return

    _manually_execute_recipe(r)

    ds_target = xr.open_zarr(tmp_target.get_mapper(), consolidated=True)

    # chunk validation
    sequence_chunks = ds_target.chunks["time"]
    if target_chunks is None:
        target_chunks = {}
    seq_chunk_len = target_chunks.pop("time", None) or (items_per_file * inputs_per_chunk)
    # we expect all chunks but the last to have the expected size
    assert all([item == seq_chunk_len for item in sequence_chunks[:-1]])
    for other_dim, chunk_len in target_chunks.items():
        all([item == chunk_len for item in ds_target.chunks[other_dim][:-1]])

    ds_target.load()
    ds_expected = daily_xarray_dataset.compute()

    if process_input is not None:
        # check that the process_input hook made some changes
        assert not ds_target.identical(ds_expected)
        # apply these changes to the expected dataset
        ds_expected = process_input(ds_expected)
    if process_chunk is not None:
        # check that the process_chunk hook made some changes
        assert not ds_target.identical(ds_expected)
        # apply these changes to the expected dataset
        ds_expected = process_chunk(ds_expected)

    assert ds_target.identical(ds_expected)


def test_NetCDFtoZarrSequentialRecipeNoTarget(
    daily_xarray_dataset, netcdf_local_paths, tmp_target, tmp_cache
):

    paths, items_per_file = netcdf_local_paths
    r = recipe.NetCDFtoZarrSequentialRecipe(
        input_urls=paths, sequence_dim="time", inputs_per_chunk=1, nitems_per_input=items_per_file,
    )

    with pytest.raises(UninitializedTargetError):
        r.cache_input(next(r.iter_inputs()))


def test_NetCDFtoZarrMultiVarSequentialRecipe(
    daily_xarray_dataset, netcdf_local_paths_by_variable, tmp_target, tmp_cache
):
    paths, items_per_file, fnames_by_variable, path_format = netcdf_local_paths_by_variable
    pattern = VariableSequencePattern(
        path_format, keys={"variable": ["foo", "bar"], "n": list(range(len(paths) // 2))}
    )
    r = recipe.NetCDFtoZarrMultiVarSequentialRecipe(
        input_pattern=pattern,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=items_per_file,
        target=tmp_target,
        input_cache=tmp_cache,
    )
    _manually_execute_recipe(r)

    ds_target = xr.open_zarr(tmp_target.get_mapper(), consolidated=True).compute()
    assert ds_target.identical(daily_xarray_dataset)


def test_NetCDFtoZarrMultiVarSequentialRecipeIncremental(
    daily_xarray_dataset, netcdf_local_paths_by_variable, tmp_target, tmp_cache
):
    paths, items_per_file, fnames_by_variable, path_format = netcdf_local_paths_by_variable
    pattern1 = VariableSequencePattern(
        path_format, keys={"variable": ["foo", "bar"], "n": list(range(len(paths) // 4))}
    )
    r1 = recipe.NetCDFtoZarrMultiVarSequentialRecipe(
        input_pattern=pattern1,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=items_per_file,
        target=tmp_target,
        input_cache=tmp_cache,
    )

    processed_input_urls = [v for k, v in pattern1]
    pattern2 = VariableSequencePattern(
        path_format, keys={"variable": ["foo", "bar"], "n": list(range(len(paths) // 2))}
    )
    r2 = recipe.NetCDFtoZarrMultiVarSequentialRecipe(
        processed_input_urls=processed_input_urls,
        input_pattern=pattern2,
        sequence_dim="time",
        inputs_per_chunk=1,
        nitems_per_input=items_per_file,
        target=tmp_target,
        input_cache=tmp_cache,
    )
    # check that r2 needs r1 to be executed first
    with pytest.raises(FileNotFoundError):
        _manually_execute_recipe(r2)
    _manually_execute_recipe(r1)
    _manually_execute_recipe(r2)

    ds_target = xr.open_zarr(tmp_target.get_mapper(), consolidated=True).compute()
    assert ds_target.identical(daily_xarray_dataset)
