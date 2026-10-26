CXXFLAGS = -std=c++17 -O2 -Wall -Wextra -Werror -pedantic
CPPFLAGS += -I.. -I.

all: server client

server: server.cpp ../protocol.h common.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ server.cpp

client: client.cpp stats.cpp stats.h report.cpp report.h ../protocol.h common.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ client.cpp stats.cpp report.cpp

test_protocol: ../tests/test_protocol.cpp ../tests/check.h ../protocol.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ ../tests/test_protocol.cpp

test_stats: ../tests/test_stats.cpp ../tests/check.h stats.cpp stats.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ ../tests/test_stats.cpp stats.cpp

test_report: ../tests/test_report.cpp ../tests/check.h report.cpp report.h stats.h
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -o $@ ../tests/test_report.cpp report.cpp

test: test_protocol test_stats test_report
	./test_protocol && ./test_stats && ./test_report

clean:
	rm -f server client test_protocol test_stats test_report

.PHONY: all test clean
