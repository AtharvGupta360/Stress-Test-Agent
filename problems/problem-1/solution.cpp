#include <bits/stdc++.h>
using namespace std;

#define int long long
#define vll vector<long long>
#define yes cout << "YES\n"
#define no cout << "NO\n"
#define all(x) (x).begin(), (x).end()

int ceil_div(int a, int b){ return (a + b - 1)/ b ;}
int gcd(int a, int b) { if(a == 0ll) { return b ;} return gcd(b % a , a);}

/*

*/



void solve() {
     int n;
     cin >> n;

     vector<int> v(n);
    for(int i=0;i<n;i++){
        cin >> v[i];
    }
     
    if(n & 1){
        no;
        return;
    }
    int r = v[0] - 1;
    int l = v[1] + 1;
    for(int i=1; i<n;i+=2){
        if(l > r){
            no;
            return;
        }

        l = max(l, v[i] +1);
        r = min(r, v[i-1] - 1);

       // cout << l << " " << r << endl;
    }

    yes;

}   

int32_t main() {
    ios_base::sync_with_stdio(false);
    cin.tie(NULL);
    cout.tie(NULL);

    #ifndef ONLINE_JUDGE
        freopen("input.txt", "r", stdin);
        freopen("output.txt", "w", stdout);
        freopen("Error.txt", "w", stderr);
    #endif

    int t = 1;
     cin >> t;
    while (t--) {
        solve();
    }

    return 0;
}