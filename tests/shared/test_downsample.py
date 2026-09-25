from shared.downsample import downsample_rows, lttb


def test_endpoints_are_always_kept():
    pts = [(float(i), float(i)) for i in range(1000)]
    out = lttb(pts, 50)
    assert len(out) == 50
    assert out[0] == pts[0] and out[-1] == pts[-1]


def test_nothing_to_do_when_already_under_threshold():
    pts = [(0.0, 0.0), (1.0, 1.0)]
    assert lttb(pts, 100) == pts


def test_lttb_keeps_a_spike_that_stride_sampling_would_drop():
    # The whole reason the spec says LTTB and not stride sampling: a forecast
    # error blow-up must survive downsampling.
    pts = [(float(i), 1.0) for i in range(1000)]
    pts[501] = (501.0, 999.0)
    kept = lttb(pts, 50)
    assert any(y > 500 for _, y in kept), "the spike vanished"

    stride = pts[:: len(pts) // 50]
    assert not any(y > 500 for _, y in stride), "test is meaningless if stride keeps it too"


def test_rows_are_returned_whole_not_just_the_plotted_columns():
    rows = [{"t": i, "v": i * i, "label": f"row-{i}"} for i in range(500)]
    out = downsample_rows(rows, 25, x="t", y="v")
    assert len(out) <= 25
    assert all(set(r) == {"t", "v", "label"} for r in out)
    assert out[0]["t"] == 0 and out[-1]["t"] == 499


def test_non_numeric_rows_fall_back_to_even_sampling_with_endpoints():
    rows = [{"name": f"n{i}"} for i in range(100)]
    out = downsample_rows(rows, 10)
    assert len(out) == 10
    assert out[0] == rows[0] and out[-1] == rows[-1]


def test_named_columns_that_are_not_numeric_do_not_raise():
    rows = [{"t": f"ts-{i}", "v": None} for i in range(100)]
    out = downsample_rows(rows, 10, x="t", y="v")
    assert len(out) == 10
