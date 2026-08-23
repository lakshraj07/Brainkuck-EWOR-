# NetBF 1:1 chat server
#
#   *        listen + accept (cell 0 == 0 means --port, default 4242)
#   [-]+     cell 0 = 1, shared loop flag
#   then recv / send run together
#   [>v.<]   recv a byte, print it
#   [>>,^<<] read a keystroke, send it
#
# Listener closes after the first accept, so a third peer is refused.

*[-]+|[>v.<]|[>>,^<<]
