// Planted bug: a greedy that looks reasonable and is wrong.
//
// Sorting descending and dropping each item into whichever group is currently
// lighter is the standard first instinct for this problem. It gets the samples
// right and fails on inputs that need a globally balanced choice.
#include <algorithm>
#include <iostream>
#include <vector>

int main() {
    int n;
    std::cin >> n;
    std::vector<int> a(n);
    for (int i = 0; i < n; i++) std::cin >> a[i];

    std::sort(a.begin(), a.end(), std::greater<int>());

    long long groupA = 0, groupB = 0;
    for (int i = 0; i < n; i++) {
        if (groupA <= groupB) {
            groupA += a[i];
        } else {
            groupB += a[i];
        }
    }

    std::cout << std::abs(groupA - groupB) << std::endl;
    return 0;
}
