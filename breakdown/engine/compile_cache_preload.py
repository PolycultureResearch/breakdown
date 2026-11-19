"""Imported by the multiprocessing forkserver before it forks any sampling
chain (see `model.preload_forkserver_modules`), so that every chain process
starts with the numba cache hardened. Importing it is the whole point; see
`compile_cache`."""

from breakdown.engine.compile_cache import ensure_safe_compile_cache

ensure_safe_compile_cache()
