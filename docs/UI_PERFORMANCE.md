# Transaction picker performance

The edit and split pickers now look up transactions by ID instead of scanning the list for each option. The lookup is rebuilt from the current rows on each render; no financial data is added to a shared cache.

Local synthetic benchmark: 10,000 transactions, median of three runs, including lookup construction and currency/date formatting. The new formatter was extracted from the actual editor code. Every generated label matched the previous formatter.

| Option formatting | Time |
| --- | --- |
| Before | 1.2838 s |
| After | 0.0053 s |

This measures server-side option formatting, not full page loading, network latency, or browser responsiveness on a physical mobile device.
