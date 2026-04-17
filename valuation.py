import argparse


def harvest_pmf(n):
    """PMF for the sum of n i.i.d. Uniform{0,...,100} harvests."""
    pmf = {0: 1.0}
    for _ in range(n):
        new_pmf = {}
        for s, p in pmf.items():
            for u in range(101):
                new_pmf[s + u] = new_pmf.get(s + u, 0) + p / 101
        pmf = new_pmf
    return pmf


def cost_at(x, pmf, Q=1.0):
    """Q * E[max(100 - x - S, 0)] — expected winter shortfall cost at avg buyback price Q."""
    return Q * sum(p * max(100 - x - s, 0) for s, p in pmf.items())


def optimal_trade(P, x=0, n=2, Q=1.0, max_abs=300):
    """Find integer trade q (+=buy, -=sell) maximizing expected profit."""
    pmf = harvest_pmf(n)
    base = cost_at(x, pmf, Q)
    best_q, best_profit = 0, 0.0
    for q in range(-max_abs, max_abs + 1):
        profit = base - cost_at(x + q, pmf, Q) - P * q
        if profit > best_profit + 1e-12 or (
            abs(profit - best_profit) < 1e-12 and abs(q) < abs(best_q)
        ):
            best_profit = profit
            best_q = q
    return best_q, best_profit


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find the profit-maximizing bean trade.")
    parser.add_argument("-P", type=float, required=True, help="current market price per bean")
    parser.add_argument("-Q", type=float, default=1.0,
                        help="expected buyback price per bean at winter (default 1.0 = govt rate)")
    parser.add_argument("-x", type=float, default=0, help="current holding (default 0)")
    parser.add_argument("-n", type=int, default=2, help="harvests remaining (default 2)")
    args = parser.parse_args()

    q, profit = optimal_trade(args.P, args.x, args.n, args.Q)
    if q > 0:
        action = f"buy {q} beans"
    elif q < 0:
        action = f"sell {-q} beans"
    else:
        action = "do nothing"

    print(f"State: holding {args.x} beans, {args.n} harvest(s) remaining")
    print(f"Now price: ${args.P:.4f}/bean; expected winter buyback: ${args.Q:.4f}/bean")
    print(f"Optimal action: {action}")
    print(f"Expected profit: ${profit:.4f}")
