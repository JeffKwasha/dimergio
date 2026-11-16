"""Behavior of the IO-wait sampler: domain summing, per-event share, idle
windows, and the one-window lag that makes concurrent reads pay their share."""

from dimergio.collector import IOWaitSampler


def _sampler(domains, br2domain):
    s = IOWaitSampler(domains, br2domain, 10)
    s._prev_ticks = {dev: 0 for devices in domains for dev in devices}
    return s


def test_share_divides_domain_busy_time_across_events():
    s = _sampler([["dm-0"]], [0])
    s._prev_ticks["dm-0"] = 100
    s.record_events(0, 4)

    s._sample_once({"dm-0": 180})

    assert s._share_ms[0] == 20.0
    assert s.get_busy(0) == 0.02
    assert s.total_iowait_sec == 0.08


def test_domain_sums_every_backing_device():
    """Branches on one btrfs pool share a device set; their busy time adds up."""
    s = _sampler([["dm-0", "dm-1"]], [0])
    s._prev_ticks.update({"dm-0": 10, "dm-1": 50})
    s.record_events(0, 2)

    s._sample_once({"dm-0": 16, "dm-1": 54})

    assert s._share_ms[0] == 5.0
    assert s.total_iowait_sec == 0.01


def test_domains_are_independent():
    s = _sampler([["dm-0"], ["dm-1"]], [0, 1])
    s.record_events(1, 2)

    s._sample_once({"dm-0": 500, "dm-1": 40})

    assert s.get_busy(0) == 0.0  # no reads observed on domain 0
    assert s.get_busy(1) == 0.02
    assert s.total_iowait_sec == 0.54


def test_idle_window_reports_zero_and_keeps_running_total():
    s = _sampler([["dm-0"]], [0])
    s.record_events(0, 1)
    s._sample_once({"dm-0": 30})
    assert s.get_busy(0) == 0.03

    s._sample_once({"dm-0": 45})

    assert s.get_busy(0) == 0.0  # no events in that window -> no charge
    assert s.total_iowait_sec == 0.045


def test_unknown_device_is_skipped():
    s = _sampler([["dm-9"]], [0])
    s.record_events(0, 1)

    s._sample_once({"dm-0": 999})

    assert s.get_busy(0) == 0.0
    assert s.total_iowait_sec == 0.0


def test_counter_reset_does_not_go_negative():
    s = _sampler([["dm-0"]], [0])
    s._prev_ticks["dm-0"] = 500
    s.record_events(0, 1)

    s._sample_once({"dm-0": 10})

    assert s.get_busy(0) == 0.0
    assert s.total_iowait_sec == 0.0


def test_interval_floor():
    s = _sampler([["dm-0"]], [0])
    s.set_interval_ms(1)
    assert s.interval_ms == IOWaitSampler.MIN_INTERVAL_MS
