# Lambda Spike Detection

Detects runaway Lambda costs within minutes by comparing current activity against historical baseline.

## How It Works

```
+-----------------------------------------------------------------+
|                        TIME WINDOWS                              |
+-----------------------------------------------------------------+
|                                                                  |
|  BASELINE WINDOW (7 days)                    SHORT WINDOW (5min) |
|  <------------------------------------------>|<--->|             |
|  |                                            |    |             |
|  7 days ago                              5min ago  now           |
|                                                                  |
|  Total: 10,080 invocations                   180 invocations     |
|  Rate:  10,080 / (7*24*60) = 0.1/min         180 / 5 = 36/min    |
|                                                                  |
+-----------------------------------------------------------------+

SPIKE RATIO = current_rate / baseline_rate = 36 / 0.1 = 360x

If SPIKE RATIO >= THRESHOLD (default 10x) -> ALERT!
```

## Algorithm Steps

1. **Query SHORT window** (default 5 min): Get invocations from CloudWatch
2. **Query BASELINE window** (default 7 days): Get total invocations
3. **Calculate rates per minute**:
   - `current_rate = short_invocations / short_minutes`
   - `baseline_rate = baseline_invocations / baseline_minutes`
4. **Compute spike ratio**: `current_rate / baseline_rate`
5. **Compare to threshold**: If ratio >= 10x, trigger alert
6. **Project daily cost**: What it would cost if spike continues 24h

## Real-World Example

```
Function: data-transformer (256MB)

Historical pattern (December):
  - 52,000 invocations/day = 36/min
  - Cost: $65/day

Current state (January - idle):
  - 200 invocations/day = 0.14/min
  - Cost: $0.01/day

If December spike happens again:
  Baseline (7 days at idle): 0.14/min
  Sudden spike:              36/min
  Ratio:                     257x
  Threshold:                 10x

  Result: SPIKE DETECTED in 5 minutes!
  Alert shows: "Projected $65/day if continues"
```

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `spike_threshold` | 10 | Alert if rate >= Nx baseline |
| `spike_window` | 5 min | How recent to check |
| `baseline_hours` | 168 (7d) | Historical comparison period |

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No baseline, has current activity | Ratio = 999 (new function alert) |
| No current activity | No spike (idle function) |
| Baseline higher than current | No spike (activity decreased) |

## Detection Speed

| Spike Window | Detection Time |
|--------------|----------------|
| 5 min (default) | Alert within 5 minutes of spike |
| 1 min | Alert within 1 minute (more sensitive) |
| 15 min | Alert within 15 minutes (fewer false positives) |
