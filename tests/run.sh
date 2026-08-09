#!/bin/sh
# Build and run the host-side tests.
#
# They compile the firmware's own headers with a normal compiler, so they need
# no board attached and no PlatformIO — just a C++17 compiler. The binary goes
# under .pio/ to keep it out of the way and out of git.
set -e
cd "$(dirname "$0")/.."
mkdir -p .pio/tests
c++ -std=c++17 -O2 -Wall -Wextra -Werror -o .pio/tests/layout_test tests/layout_test.cpp
exec .pio/tests/layout_test "$@"
