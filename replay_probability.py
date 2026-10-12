"""Historical paper risk overlay, separate from saved model weights/training."""
import math
from polymarket_pipeline import count_range_probability

POLICY_VERSION = "remaining-count-prior24h-v1"


def guarded_forecast(model, features, ranges, prior_rate):
    current, elapsed, remaining = map(float, features[:3])
    raw = float(model.predict(features))
    if not all(math.isfinite(value) for value in (current, elapsed, remaining, raw, float(prior_rate))):
        raise ValueError("概率计算输入非有限值，已停止纸面模拟")
    # A five-minute burst is not evidence for a week at the same rate.
    # The prior is computed solely from markets closed before this one opened.
    prior = max(0.0, float(prior_rate))
    supported = prior > 0 or elapsed >= 24
    rate = (current + 24 * prior) / max(elapsed + 24, 1e-9)
    center = current + rate * remaining
    stabilized = list(features)
    stabilized[3] = center
    proposed = float(model.predict(stabilized))
    if not math.isfinite(proposed):
        raise ValueError("稳定速度后的模型预测非有限值，已停止纸面模拟")
    horizon = remaining / max(elapsed + remaining, 1)
    variance = max(0.0, rate * remaining) + (20 * horizon) ** 2
    # Keep regression corrections within the supported count range; relax as
    # observations accumulate. This is a risk guard, not fitted calibration.
    radius = 2 * math.sqrt(variance) * elapsed / max(elapsed + 24, 1)
    forecast = max(current, min(center + radius, max(center - radius, proposed)))
    remaining_mean = max(0.0, forecast - current)
    variance = max(remaining_mean, variance)
    probabilities = []
    for _, lower, upper in ranges:
        if upper is not None and upper < current:
            probabilities.append(0.0)
        else:
            probabilities.append(min(1.0, max(0.0, count_range_probability(
                remaining_mean, variance, max(0, int((lower or 0) - current)),
                None if upper is None else int(upper - current)))))
    return forecast, probabilities, dict(
        policy=POLICY_VERSION, raw_forecast=raw, prior_rate=prior,
        stabilized_projection=center, variance=variance,
        support_lower=max(current, forecast - 3 * math.sqrt(variance)),
        support_upper=forecast + 3 * math.sqrt(variance),
        entry_supported=supported, minimum_entry_probability=0.10,
        probability_mass=sum(probabilities), calibrated=False)
