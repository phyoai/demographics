import random

def redistribute_to_zero_groups(data: dict[str, float], seed: int | None = None) -> dict[str, float]:
    """
    For every group with 0.0:
    - add a random value between 0 and 1
    - subtract that same value from the highest available group
    - if needed, keep taking from next highest group(s)

    Returns a new dict, original dict is not changed.
    """
    if seed is not None:
        random.seed(seed)

    result = data.copy()

    # groups that need to be filled
    zero_groups = [k for k, v in result.items() if v == 0.0]

    for zero_group in zero_groups:
        amount_needed = round(random.uniform(0, 1), 2)

        if amount_needed == 0:
            continue

        # sort donor groups by current value, highest first
        donor_groups = sorted(
            [k for k, v in result.items() if k != zero_group and v > 0],
            key=lambda k: result[k],
            reverse=True
        )

        remaining = amount_needed

        for donor in donor_groups:
            if remaining <= 0:
                break

            take = min(result[donor], remaining)
            result[donor] = round(result[donor] - take, 2)
            remaining = round(remaining - take, 2)

        # add only the amount actually taken
        given = round(amount_needed - remaining, 2)
        result[zero_group] = round(result[zero_group] + given, 2)

    return result