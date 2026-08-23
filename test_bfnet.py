#!/usr/bin/env python3
from __future__ import annotations

import io
import os
import select
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

from bfnet import BFError, Machine

ROOT = Path(__file__).resolve().parent
PROGRAMS = ROOT / "programs"
PY = sys.executable


def run_source(source: str, stdin: bytes = b"") -> bytes:
    machine = Machine(
        source,
        stdin=io.BytesIO(stdin),
        stdout=io.BytesIO(),
        stderr=io.StringIO(),
    )
    machine.run()
    assert isinstance(machine.stdout, io.BytesIO)
    return machine.stdout.getvalue()


def run_file(name: str, stdin: bytes = b"") -> bytes:
    return run_source((PROGRAMS / name).read_text(), stdin)


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def start_peer(program: str, port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [PY, "-u", str(ROOT / "bfnet.py"), str(PROGRAMS / program), "--port", str(port)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
        env=dict(os.environ, PYTHONUNBUFFERED="1"),
    )


def wait_stderr(proc: subprocess.Popen, needle: bytes, timeout: float = 3.0) -> bytes:
    assert proc.stderr is not None
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        ready, _, _ = select.select([proc.stderr], [], [], remaining)
        if not ready:
            break
        chunk = os.read(proc.stderr.fileno(), 4096)
        if not chunk:
            break
        buf += chunk
        if needle in buf:
            return buf
    raise AssertionError("did not see %r in stderr: %r" % (needle, buf))


def read_stdout(proc: subprocess.Popen, n: int, timeout: float = 3.0) -> bytes:
    assert proc.stdout is not None
    buf = b""
    deadline = time.time() + timeout
    while len(buf) < n and time.time() < deadline:
        remaining = deadline - time.time()
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            break
        chunk = os.read(proc.stdout.fileno(), n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


class InterpreterTests(unittest.TestCase):
    def test_hello_world(self) -> None:
        self.assertEqual(run_file("hello.bf"), b"Hello, World!")

    def test_cat(self) -> None:
        self.assertEqual(run_file("cat.bf", b"abc"), b"abc")

    def test_cell_wraps_at_256(self) -> None:
        self.assertEqual(run_source("+" * 256 + "."), b"\x00")

    def test_unmatched_open_bracket(self) -> None:
        with self.assertRaises(BFError):
            run_source("[")

    def test_unmatched_close_bracket(self) -> None:
        with self.assertRaises(BFError):
            run_source("]")

    def test_pointer_cannot_go_left_of_zero(self) -> None:
        with self.assertRaises(BFError):
            run_source("<")

    def test_comments_are_ignored(self) -> None:
        self.assertEqual(run_source("hello + . world"), b"\x01")

    def test_hash_comments_can_contain_ops(self) -> None:
        self.assertEqual(run_source("# this has . [ ] v |\n+."), b"\x01")


class ChatTests(unittest.TestCase):
    def tearDown(self) -> None:
        for attr in ("server", "client"):
            proc = getattr(self, attr, None)
            if proc is None:
                continue
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()

    def start_pair(self) -> int:
        port = free_port()
        self.server = start_peer("server.bf", port)
        wait_stderr(self.server, b"listening")
        self.client = start_peer("client.bf", port)
        wait_stderr(self.server, b"peer connected")
        wait_stderr(self.client, b"connected")
        return port

    def test_alice_to_bob(self) -> None:
        self.start_pair()
        assert self.server.stdin and self.client.stdout
        self.server.stdin.write(b"hi\n")
        self.server.stdin.flush()
        self.assertEqual(read_stdout(self.client, 3), b"hi\n")

    def test_bob_to_alice(self) -> None:
        self.start_pair()
        assert self.client.stdin and self.server.stdout
        self.client.stdin.write(b"yo\n")
        self.client.stdin.flush()
        self.assertEqual(read_stdout(self.server, 3), b"yo\n")

    def test_full_duplex(self) -> None:
        self.start_pair()
        assert self.server.stdin and self.client.stdin
        self.server.stdin.write(b"A")
        self.client.stdin.write(b"B")
        self.server.stdin.flush()
        self.client.stdin.flush()
        self.assertEqual(read_stdout(self.client, 1), b"A")
        self.assertEqual(read_stdout(self.server, 1), b"B")

    def test_empty_line_does_not_crash(self) -> None:
        self.start_pair()
        assert self.server.stdin and self.client.stdout
        self.server.stdin.write(b"\n")
        self.server.stdin.flush()
        self.assertEqual(read_stdout(self.client, 1), b"\n")

    def test_third_peer_is_refused(self) -> None:
        port = self.start_pair()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.4)

    def test_client_fails_when_server_absent(self) -> None:
        port = free_port()
        client = start_peer("client.bf", port)
        try:
            code = client.wait(timeout=5)
            self.assertNotEqual(code, 0)
            err = client.stderr.read() if client.stderr else b""
            self.assertIn(b"connect failed", err)
        finally:
            if client.poll() is None:
                client.kill()
                client.wait()
            for stream in (client.stdin, client.stdout, client.stderr):
                if stream is not None:
                    stream.close()

    def test_peer_leave_unblocks_the_other_side(self) -> None:
        self.start_pair()
        assert self.client.stdin
        self.client.stdin.close()
        code = self.server.wait(timeout=3)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
