"""IO-wait billing: domain summing, per-file split, and window accounting."""

from dimergio.collector import IOWaitSampler


def _sampler(domains, br2domain, charges=None):
    s = IOWaitSampler(
        domains,
        br2domain,
        10,
        on_charge=(charges.append if charges is not None else None),
    )
    s._prev_ticks = {dev: 0 for devices in domains for dev in devices}
    return s


def test_window_busy_time_is_split_across_files():
    charges = []
    s = _sampler([["dm-0"]], [0], charges)
    s._prev_ticks["dm-0"] = 100
    s.record_event(0, ("a", 1), 3)
    s.record_event(0, ("b", 1), 1)

    s._sample_once({"dm-0": 180})

    assert charges == [{("a", 1): 0.06, ("b", 1): 0.02}]


def test_a_single_sparse_read_is_billed_the_whole_window():
    """One read per window still pays the busy time of that window."""
    charges = []
    s = _sampler([["dm-0"]], [0], charges)
    s._prev_ticks["dm-0"] = 10
    s.record_event(0, ("a", 1))

    s._sample_once({"dm-0": 40})

    assert charges == [{("a", 1): 0.03}]


def test_events_are_not_billed_a_window_late():
    """A read is never billed to the following window's busy time."""
    charges = []
    s = _sampler([["dm-0"]], [0], charges)
    s._prev_ticks["dm-0"] = 0
    s.record_event(0, ("a", 1))
    s._sample_once({"dm-0": 20})
    assert charges == [{("a", 1): 0.02}]

    s._sample_once({"dm-0": 60})
    assert charges == [{("a", 1): 0.02}]  # idle window bills nobody


def test_idle_window_bills_nobody_and_keeps_the_total():
    charges = []
    s = _sampler([["dm-0"]], [0], charges)
    s._sample_once({"dm-0": 30})
    assert charges == []

    s._sample_once({"dm-0": 45})
    assert s.total_iowait_sec == 0.045


def test_domain_sums_every_backing_device():
    charges = []
    s = _sampler([["dm-0", "dm-1"]], [0], charges)
    s._prev_ticks.update({"dm-0": 10, "dm-1": 50})
    s.record_event(0, ("a", 1), 2)

    s._sample_once({"dm-0": 16, "dm-1": 54})

    assert charges == [{("a", 1): 0.01}]
    assert s.total_iowait_sec == 0.01


def test_domains_bill_separately():
    charges = []
    s = _sampler([["dm-0"], ["dm-1"]], [0, 1], charges)
    s.record_event(1, ("b", 2), 2)

    s._sample_once({"dm-0": 500, "dm-1": 40})

    assert charges == [{("b", 2): 0.04}]
    assert s.total_iowait_sec == 0.54


def test_unknown_device_is_skipped():
    charges = []
    s = _sampler([["dm-9"]], [0], charges)
    s.record_event(0, ("a", 1))

    s._sample_once({"dm-0": 999})

    assert charges == []
    assert s.total_iowait_sec == 0.0


def test_counter_reset_does_not_go_negative():
    charges = []
    s = _sampler([["dm-0"]], [0], charges)
    s._prev_ticks["dm-0"] = 500
    s.record_event(0, ("a", 1))

    s._sample_once({"dm-0": 10})

    assert charges == []
    assert s.total_iowait_sec == 0.0


def test_mmap_window_counts_as_many_reads():
    charges = []
    s = _sampler([["dm-0"]], [0], charges)
    s._prev_ticks["dm-0"] = 0
    s.record_event(0, ("a", 1), 9)
    s.record_event(0, ("b", 1), 1)

    s._sample_once({"dm-0": 100})

    assert charges == [{("a", 1): 0.09, ("b", 1): 0.01}]


def test_interval_floor():
    s = _sampler([["dm-0"]], [0])
    s.set_interval_ms(1)
    assert s.interval_ms == IOWaitSampler.MIN_INTERVAL_MS
