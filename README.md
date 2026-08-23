# 1:1 Chat in Brainfuck

EWOR builder case study. 

## Summary

This project implements a 1:1 chat client and server in which the message-passing
logic executes as Brainfuck. Brainfuck has no native networking, so the interpreter
is extended with a minimal, explicitly documented set of instructions for TCP I/O.
The chat protocol itself runs on top of that extension, not around it.

## Constraint

Brainfuck defines eight instructions: `> < + - . , [ ]`. Input and output are
limited to single bytes via `,` and `.`, targeting one stdin/stdout stream. There
is no instruction for opening a connection and no concept of a second party. Read
literally, "a server and a client in Brainfuck" is not satisfiable by the language
as specified.

## Decision

The interpreter (`bfnet.py`) is extended with five instructions: listen/accept,
connect, send, receive, and a control instruction for concurrent execution. This
dialect is referred to as NetBF. The chat protocol (`server.bf`, `client.bf`) is
written using these instructions and executes on the interpreter; it is not
implemented in the host language.

This keeps the constraint that the server and client are the Brainfuck programs:
deleting `server.bf` or `client.bf` deletes the chat, since no networking logic
exists outside them. An alternative was considered and rejected: leave Brainfuck
unmodified, run a vanilla `,[.,]` program, and perform all networking in a host
process that pipes bytes to it. That approach was set aside because the `.bf` file
would then be decorative, it would run identically regardless of what the host
decided to connect it to, and none of the actual chat behavior (who connects to
whom, when, in which direction) would be determined by the Brainfuck program.

`hello.bf` and `cat.bf` are kept in the repository as unmodified vanilla programs
and run on the same interpreter, to confirm the extension is additive to the base
language rather than a replacement of it.

## Architecture

`bfnet.py` is the host: it parses NetBF source, drives the tape, and owns the
socket calls (`socket.listen`, `.accept`, `.connect`, `.send`, `.recv`).
`server.bf` and `client.bf` run on top of it and constitute the chat: they decide
when to listen or connect, when to send, when to receive, when to print.

```
terminal 1                          terminal 2
python3 bfnet.py server.bf  <--TCP-->  python3 bfnet.py client.bf
```

Full duplex is implemented with the `|` control instruction, which splits a
program into a setup section and two sections that run concurrently on a shared
tape with independent pointers: one receiving and printing, one reading input and
sending. Each is cooperatively scheduled so that a blocking read on one side does
not stall the other.

## Usage

Requires Python 3.9+, no external packages.

```bash
# terminal 1
python3 bfnet.py programs/server.bf

# terminal 2
python3 bfnet.py programs/client.bf
```

Each keystroke is sent as it is typed; there is no line buffering on the wire.
Ctrl-C or Ctrl-D exits the local process and closes the socket, which unblocks the
peer's `recv` and ends its process as well.

Default port is `4242` on localhost. Override without editing the `.bf` files:

```bash
python3 bfnet.py programs/server.bf --port 4747
python3 bfnet.py programs/client.bf --port 4747
```

## NetBF Reference

Vanilla instructions: `> < + - . , [ ]`. NetBF adds:

| Op | Meaning |
|---|---|
| `*` | Listen and accept on the port held in the current cell. Cell value `0` means "use `--port`" (default 4242). The listener is closed immediately after accepting. |
| `~` | Connect to `--host`:`port`, same cell-`0` rule. Retries for a short period, so the client may be started before the server. |
| `^` | Send the current cell as one byte. |
| `v` | Receive one byte into the current cell. If the peer has closed the connection, this halts the program rather than blocking indefinitely. |
| `\|` | Split the program into `setup \| recv \| send`. Setup runs once; `recv` and `send` then run concurrently, as described under Architecture. |

`#` is a host-level convenience, not a NetBF instruction: everything after it on a
line is discarded before parsing. This is necessary because vanilla Brainfuck
ignores any character that is not an instruction, so an English comment
containing a stray `.` or `[` would otherwise be interpreted as code.

`server.bf` and `client.bf` differ by one character:

```
*[-]+|[>v.<]|[>>,^<<]    server
~[-]+|[>v.<]|[>>,^<<]    client
```

Cell 0 is the loop flag. Cell 1 holds the most recently received byte. Cell 2
holds the most recently typed byte.

## Design Decisions

| Decision | Choice |
|---|---|
| Transport | TCP, bound to `127.0.0.1` |
| Topology | One listener, one connector; listener closes after accept |
| Duplex | Two cooperatively scheduled threads within a single interpreter process |
| Framing | One byte at a time; a newline is treated as any other byte |
| Termination | stdin EOF or Ctrl-C; socket closure unblocks the peer |
| Encoding | Raw bytes; cells wrap modulo 256 |
| Out of scope | Accounts, rooms, message history, TLS, a GUI |

The 1:1 constraint is structural rather than enforced by a check: the listen
socket is closed the instant the first peer connects, so a third connection
attempt is refused by the operating system before NetBF observes it.

## Testing

```bash
python3 -m unittest test_bfnet.py -v
```

Interpreter tests cover vanilla Brainfuck in isolation: `hello.bf`, `cat.bf`,
cell wraparound, unmatched brackets, comment stripping.

Chat tests run `server.bf` and `client.bf` as real subprocesses over a real
socket: message delivery in each direction, both directions simultaneously, an
empty line, a refused third connection, a client failing cleanly when no server
is listening, and one peer's disconnect unblocking the other.

## Repository Layout

```
bfnet.py             Interpreter and CLI
programs/server.bf    NetBF chat server
programs/client.bf    NetBF chat client
programs/cat.bf       Vanilla Brainfuck, used by interpreter tests
programs/hello.bf      Vanilla Brainfuck, sanity check for the base language
test_bfnet.py
```
