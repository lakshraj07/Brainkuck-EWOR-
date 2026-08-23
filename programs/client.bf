# NetBF 1:1 chat client
#
#   ~        connect to --host:port (cell 0 == 0 means --port)
#   [-]+     cell 0 = 1, shared loop flag
#   [>v.<]   recv a byte, print it
#   [>>,^<<] read a keystroke, send it
#
# Same chat body as server.bf. The only difference is * vs ~.

~[-]+|[>v.<]|[>>,^<<]
