"""Stand-in for src.pressure whose self-test fails."""
import sys

if __name__ == "__main__":
    print("selftest: case 2 differs by 0.4 Pa", file=sys.stderr)
    sys.exit(1)
