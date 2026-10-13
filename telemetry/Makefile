CXX      ?= g++
CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Werror -pedantic
CPPFLAGS += -I..

all: server client

server: server.cpp ../protocol.h common.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ server.cpp

client: client.cpp stats.cpp stats.h ../protocol.h common.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ client.cpp stats.cpp

test_protocol: ../tests/test_protocol.cpp ../protocol.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ ../tests/test_protocol.cpp

test_stats: ../tests/test_stats.cpp stats.cpp stats.h
	$(CXX) $(CPPFLAGS) -I. $(CXXFLAGS) -o $@ ../tests/test_stats.cpp stats.cpp

test: test_protocol test_stats
	./test_protocol && ./test_stats

clean:
	rm -f server client test_protocol test_stats

.PHONY: all test clean
