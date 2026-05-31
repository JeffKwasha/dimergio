from dimergio.collector import _LINE_RE


class TestFatraceLineRegex:
    def test_standard_line_with_uid(self):
        line = "1748573021.456789 myapp(12345) [1000:1000]: R /mnt/pool/Data/file.ba2"
        m = _LINE_RE.match(line)
        assert m is not None
        assert m.group("ts") == "1748573021.456789"
        assert m.group("proc") == "myapp"
        assert m.group("pid") == "12345"
        assert m.group("uid") == "1000"
        assert m.group("gid") == "1000"
        assert m.group("event") == "R"
        assert m.group("path") == "/mnt/pool/Data/file.ba2"

    def test_write_event_is_parsed(self):
        line = "1748573021.456789 myapp(123) [0:0]: W /mnt/pool/save.dat"
        m = _LINE_RE.match(line)
        assert m is not None
        assert m.group("event") == "W"

    def test_open_event(self):
        line = "1748573022.000000 launcher(456) [1000:1000]: O /mnt/pool/config.ini"
        m = _LINE_RE.match(line)
        assert m is not None
        assert m.group("event") == "O"
        assert m.group("proc") == "launcher"

    def test_deeply_nested_path(self):
        line = (
            "1748573100.123456 myapp(789) [1000:1000]: R "
            "/mnt/pool/very/deep/nested/directory/structure/file.dat"
        )
        m = _LINE_RE.match(line)
        assert m is not None
        assert m.group("path") == "/mnt/pool/very/deep/nested/directory/structure/file.dat"

    def test_process_name_with_dash(self):
        line = "1748573021.000000 my-app(100) [1000:1000]: R /mnt/pool/file.dat"
        m = _LINE_RE.match(line)
        assert m is not None
        assert m.group("proc") == "my-app"

    def test_single_digit_fields(self):
        line = "1000000.000000 a(1) [0:0]: R /f"
        m = _LINE_RE.match(line)
        assert m is not None
        assert m.group("ts") == "1000000.000000"
        assert m.group("proc") == "a"
        assert m.group("pid") == "1"
        assert m.group("uid") == "0"
        assert m.group("gid") == "0"
        assert m.group("path") == "/f"

    def test_timestamp_short(self):
        line = "0.001 myapp(1) [1:1]: R /f"
        m = _LINE_RE.match(line)
        assert m is not None
        assert m.group("ts") == "0.001"

    def test_rejects_garbage_line(self):
        assert _LINE_RE.match("") is None
        assert _LINE_RE.match("not a fatrace line") is None
        assert _LINE_RE.match("1234: R /path") is None

    def test_rejects_missing_bracket(self):
        line = "1.0 myapp(123): R /path"
        m = _LINE_RE.match(line)
        assert m is None
