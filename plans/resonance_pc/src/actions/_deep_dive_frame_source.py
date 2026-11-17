from ..bridge.frame_stream import BACKEND


_ATOMIC_SOURCES = {"wgc": "atomic_wgc", BACKEND: "atomic_bridge"}


def is_stream_backend(backend):
    return isinstance(backend, str) and backend in _ATOMIC_SOURCES


def has_atomic_source(source):
    backend = source.get("capture_backend")
    return is_stream_backend(backend) and source.get("generation_source") == _ATOMIC_SOURCES[backend]


def same_stream_backend(first, second):
    backend = first.get("capture_backend", first.get("backend"))
    return is_stream_backend(backend) and backend == second.get("capture_backend", second.get("backend"))


def stream_metadata(packet):
    backend = packet["capture"].backend
    return dict(session_id=packet["session_id"], generation=packet["generation"],
                frame_time=packet["arrived_at_monotonic"], capture_backend=backend,
                generation_source=_ATOMIC_SOURCES.get(backend))
