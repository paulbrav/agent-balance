# Determinism of the seeded RNG: same seed -> same sequence; named streams are
# independent; CRN means the stream is policy-agnostic.

from sim.rng import SeedStreams


def test_same_seed_same_sequence():
    a = SeedStreams(0)
    b = SeedStreams(0)
    seq_a = [a.stream("demand").random() for _ in range(50)]
    seq_b = [b.stream("demand").random() for _ in range(50)]
    assert seq_a == seq_b


def test_named_streams_independent():
    s = SeedStreams(7)
    d = [s.stream("demand").random() for _ in range(20)]
    h = [s.stream("hazard").random() for _ in range(20)]
    assert d != h  # different derived seeds


def test_stream_is_cached():
    s = SeedStreams(1)
    assert s.stream("x") is s.stream("x")


def test_derive_is_reproducible():
    parent = SeedStreams(42)
    c1 = parent.derive("rep:0")
    c2 = SeedStreams(42).derive("rep:0")
    assert (
        [c1.stream("a").random() for _ in range(10)]
        == [c2.stream("a").random() for _ in range(10)]
    )
