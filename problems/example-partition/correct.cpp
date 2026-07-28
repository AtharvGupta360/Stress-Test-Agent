// The correct solution: enumerate every subset and keep the best split.
// Submitted with --judge-says WA it must find NO counterexample -- this is the
// false-positive test, and the one that matters most for trust.
#include <cstdlib>
#include <iostream>
#include <vector>

int main() {
    int n;
    std::cin >> n;
    std::vector<int> a(n);
    long long total = 0;
    for (int i = 0; i < n; i++) {
        std::cin >> a[i];
        total += a[i];
    }

    long long best = total;
    for (int mask = 0; mask < (1 << n); mask++) {
        long long sum = 0;
        for (int i = 0; i < n; i++) {
            if (mask & (1 << i)) sum += a[i];
        }
        best = std::min(best, std::llabs(total - 2 * sum));
    }

    std::cout << best << std::endl;
    return 0;
}
