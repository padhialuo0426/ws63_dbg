# SPDX-License-Identifier: GPL-2.0-only
# Start both client and server with the same prepared ELF first.
set pagination off
info threads
thread apply all bt 8
monitor tasks water
monitor bt all
monitor sync
detach
