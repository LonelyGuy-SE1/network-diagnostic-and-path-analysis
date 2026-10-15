#pragma once
#include <cmath>
#include <cstdio>

static int g_failures = 0;
#define CHECK(cond) do { if (!(cond)) { std::fprintf(stderr, "%s:%d: CHECK(%s) failed\n", __FILE__, __LINE__, #cond); ++g_failures; } } while (0)
#define CHECK_NEAR(a, b, eps) CHECK(std::fabs((a) - (b)) < (eps))
