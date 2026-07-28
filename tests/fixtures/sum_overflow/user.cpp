// Planted bug: accumulates into a 32-bit int, so any sum past 2^31-1 wraps.
#include <iostream>

int main() {
    int n;
    std::cin >> n;
    int sum = 0;
    for (int i = 0; i < n; i++) {
        int x;
        std::cin >> x;
        sum += x;
    }
    std::cout << sum << std::endl;
    return 0;
}
