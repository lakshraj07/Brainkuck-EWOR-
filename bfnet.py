#!/usr/bin/env python3
"""NetBF: Brainfuck plus five ops so a .bf file can be a TCP peer."""

from __future__ import annotations

import argparse
import select
import socket
import sys
import time
from typing import BinaryIO, Dict, List, Optional, TextIO

VANILLA = set("><+-.,[]")
NET = set("*~^v")
OPS = VANILLA | NET | set("|")

DEFAULT_PORT = 4242
DEFAULT_HOST = "127.0.0.1"
MAX_TAPE = 100_000
CONNECT_TRIES = 50
CONNECT_GAP = 0.05


class BFError(Exception):
    pass


def strip_hash_comments(source: str) -> str:
    # Host sugar: # to end-of-line is a comment. Vanilla BF has no comments,
    # and English prose is full of . and [ ] which are real ops.
    return "\n".join(line.split("#", 1)[0] for line in source.splitlines())


def filter_ops(source: str) -> str:
    return "".join(c for c in strip_hash_comments(source) if c in OPS)


def match_brackets(code: str) -> Dict[int, int]:
    jump: Dict[int, int] = {}
    stack: List[int] = []
    for i, c in enumerate(code):
        if c == "[":
            stack.append(i)
        elif c == "]":
            if not stack:
                raise BFError("unmatched ]")
            start = stack.pop()
            jump[start] = i
            jump[i] = start
    if stack:
        raise BFError("unmatched [")
    return jump


def split_program(code: str) -> List[str]:
    parts = code.split("|")
    if len(parts) > 3:
        raise BFError("at most two | separators (setup | recv | send)")
    return parts


class Thread:
    def __init__(self, code: str) -> None:
        self.code = code
        self.ip = 0
        self.ptr = 0
        self.jump = match_brackets(code)

    @property
    def done(self) -> bool:
        return self.ip >= len(self.code)


class Machine:
    def __init__(
        self,
        source: str,
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: TextIO,
        port: int = DEFAULT_PORT,
        host: str = DEFAULT_HOST,
        bind: str = DEFAULT_HOST,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.default_port = port
        self.host = host
        self.bind = bind
        self.tape: List[int] = [0]
        self.conn: Optional[socket.socket] = None
        self.listen: Optional[socket.socket] = None
        self.halted = False
        self.parts = split_program(filter_ops(source))

    def port_from_cell(self, ptr: int) -> int:
        value = self.tape[ptr]
        return self.default_port if value == 0 else value

    def grow(self, ptr: int) -> None:
        if ptr < 0:
            raise BFError("tape pointer moved left of 0")
        if ptr >= MAX_TAPE:
            raise BFError("tape pointer exceeded %d" % MAX_TAPE)
        while len(self.tape) <= ptr:
            self.tape.append(0)

    def status(self, message: str) -> None:
        self.stderr.write(message + "\n")
        self.stderr.flush()

    def has_fd(self, stream: BinaryIO) -> bool:
        try:
            stream.fileno()
            return True
        except (AttributeError, OSError):
            return False

    def stdin_ready(self) -> bool:
        if not self.has_fd(self.stdin):
            return True
        ready, _, _ = select.select([self.stdin], [], [], 0)
        return bool(ready)

    def sock_ready(self, sock: Optional[socket.socket]) -> bool:
        if sock is None:
            return False
        ready, _, _ = select.select([sock], [], [], 0)
        return bool(ready)

    def close_sockets(self) -> None:
        for sock in (self.conn, self.listen):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
        self.conn = None
        self.listen = None

    def listen_accept(self, ptr: int) -> None:
        if self.conn is not None:
            raise BFError("already connected")
        port = self.port_from_cell(ptr)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, port))
        sock.listen(1)
        self.listen = sock
        self.status("listening on %s:%d" % (self.bind, port))
        conn, addr = sock.accept()
        # 1:1 — drop the listener so a third peer cannot join.
        sock.close()
        self.listen = None
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.conn = conn
        self.status("peer connected %s:%d" % addr)

    def connect_peer(self, ptr: int) -> None:
        if self.conn is not None:
            raise BFError("already connected")
        port = self.port_from_cell(ptr)
        last_error: Optional[Exception] = None
        for _ in range(CONNECT_TRIES):
            try:
                sock = socket.create_connection((self.host, port), timeout=0.2)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(None)
                self.conn = sock
                self.status("connected to %s:%d" % (self.host, port))
                return
            except OSError as exc:
                last_error = exc
                time.sleep(CONNECT_GAP)
        raise BFError("connect failed: %s" % last_error)

    def send_byte(self, ptr: int) -> None:
        if self.conn is None:
            raise BFError("^ with no connection")
        try:
            self.conn.sendall(bytes([self.tape[ptr]]))
        except OSError:
            self.halted = True
            self.status("peer left")

    def recv_byte(self, ptr: int) -> None:
        if self.conn is None:
            raise BFError("v with no connection")
        try:
            data = self.conn.recv(1)
        except OSError:
            data = b""
        if not data:
            self.tape[ptr] = 0
            self.halted = True
            self.status("peer left")
            return
        self.tape[ptr] = data[0]

    def write_stdout(self, ptr: int) -> None:
        self.stdout.write(bytes([self.tape[ptr]]))
        self.stdout.flush()

    def read_stdin(self, ptr: int) -> None:
        data = self.stdin.read(1)
        if not data:
            self.halted = True
            self.status("stdin closed")
            if self.conn is not None:
                try:
                    self.conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            return
        self.tape[ptr] = data[0]

    def would_block(self, thread: Thread) -> Optional[str]:
        if thread.done:
            return None
        op = thread.code[thread.ip]
        if op == "," and self.has_fd(self.stdin) and not self.stdin_ready():
            return "stdin"
        if op == "v" and self.conn is not None and not self.sock_ready(self.conn):
            return "recv"
        if op == "*" and self.conn is None:
            return None
        return None

    def step(self, thread: Thread) -> None:
        op = thread.code[thread.ip]
        ptr = thread.ptr
        self.grow(ptr)

        if op == ">":
            thread.ptr += 1
            self.grow(thread.ptr)
        elif op == "<":
            thread.ptr -= 1
            self.grow(thread.ptr)
        elif op == "+":
            self.tape[ptr] = (self.tape[ptr] + 1) % 256
        elif op == "-":
            self.tape[ptr] = (self.tape[ptr] - 1) % 256
        elif op == ".":
            self.write_stdout(ptr)
        elif op == ",":
            self.read_stdin(ptr)
        elif op == "[":
            if self.tape[ptr] == 0:
                thread.ip = thread.jump[thread.ip]
        elif op == "]":
            if self.tape[ptr] != 0:
                thread.ip = thread.jump[thread.ip]
        elif op == "*":
            self.listen_accept(ptr)
        elif op == "~":
            self.connect_peer(ptr)
        elif op == "^":
            self.send_byte(ptr)
        elif op == "v":
            self.recv_byte(ptr)
        else:
            raise BFError("unknown op %r" % op)

        thread.ip += 1

    def run_threads(self, threads: List[Thread]) -> None:
        while not self.halted:
            live = [t for t in threads if not t.done]
            if not live:
                return

            progressed = False
            waiting = []
            for thread in live:
                need = self.would_block(thread)
                if need:
                    waiting.append(need)
                    continue
                self.step(thread)
                progressed = True
                if self.halted:
                    return

            if progressed:
                continue

            watch: List[object] = []
            if "stdin" in waiting and self.has_fd(self.stdin):
                watch.append(self.stdin)
            if "recv" in waiting and self.conn is not None:
                watch.append(self.conn)
            if not watch:
                return
            select.select(watch, [], [])

    def run(self) -> None:
        try:
            if len(self.parts) == 1:
                self.run_threads([Thread(self.parts[0])])
            elif len(self.parts) == 2:
                self.run_threads([Thread(self.parts[0]), Thread(self.parts[1])])
            else:
                setup, recv, send = self.parts
                self.run_threads([Thread(setup)])
                if not self.halted:
                    self.run_threads([Thread(recv), Thread(send)])
        finally:
            self.close_sockets()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a NetBF program (Brainfuck + TCP).",
    )
    parser.add_argument("program", help="path to a .bf file")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="port used when the current cell is 0 (default %d)" % DEFAULT_PORT,
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="connect target for ~ (default %s)" % DEFAULT_HOST,
    )
    parser.add_argument(
        "--bind",
        default=DEFAULT_HOST,
        help="listen address for * (default %s)" % DEFAULT_HOST,
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        source = open(args.program, "r", encoding="utf-8").read()
    except OSError as exc:
        sys.stderr.write("cannot read %s: %s\n" % (args.program, exc))
        return 2

    machine = None
    try:
        machine = Machine(
            source,
            # Raw stdin: a buffered read(1) would slurp the whole pipe into
            # user space, then select() on the fd would hang forever.
            stdin=sys.stdin.buffer.raw,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr,
            port=args.port,
            host=args.host,
            bind=args.bind,
        )
        machine.run()
    except BFError as exc:
        sys.stderr.write("bfnet: %s\n" % exc)
        return 1
    except KeyboardInterrupt:
        if machine is not None:
            machine.status("interrupted")
            machine.close_sockets()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
