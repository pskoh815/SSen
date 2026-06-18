"""
E6 부하 테스트 (Locust)

실행:
    locust -f tests/load/locustfile.py --host http://localhost:8000 \
           --users 20 --spawn-rate 5 --run-time 60s --headless \
           --html tests/load/report.html

목표: 20 VU, 60초, p95 < 100ms
"""
import random
from locust import HttpUser, task, between

START_DATES = ["2024-01-01", "2024-06-01", "2025-01-01", "2025-06-01", "2026-01-01"]
END_DATES   = ["2024-12-31", "2025-05-31", "2025-12-31", "2026-05-29"]
DATES_2025  = ["2025-01-02", "2025-03-15", "2025-06-10", "2025-09-01", "2025-11-28"]
CODES       = ["466100", "277810", "005930", "035420", "373220"]
THEMES      = ["반도체", "로봇", "방산", "2차전지", "원전"]


class DashboardUser(HttpUser):
    """대시보드 일반 사용자 시나리오."""
    wait_time = between(0.5, 2.0)

    def on_start(self):
        # 세션 시작 시 meta 조회 (1회)
        self.client.get("/meta/dataset")

    @task(3)
    def get_regimes(self):
        start = random.choice(START_DATES)
        end   = random.choice(END_DATES)
        self.client.get(
            f"/leaders/regimes?start={start}&end={end}",
            name="/leaders/regimes",
        )

    @task(3)
    def get_trades(self):
        start = random.choice(START_DATES)
        end   = random.choice(END_DATES)
        self.client.get(
            f"/trades?start={start}&end={end}",
            name="/trades",
        )

    @task(2)
    def get_daily_leader(self):
        d = random.choice(DATES_2025)
        self.client.get(
            f"/leaders/daily?date={d}",
            name="/leaders/daily",
        )

    @task(1)
    def get_stock_summary(self):
        code = random.choice(CODES)
        start = random.choice(START_DATES)
        self.client.get(
            f"/stocks/{code}/summary?start={start}&end=2026-05-29",
            name="/stocks/{code}/summary",
        )

    @task(1)
    def get_theme_summary(self):
        theme = random.choice(THEMES)
        self.client.get(
            f"/themes/{theme}/summary?start=2024-01-01&end=2026-05-29",
            name="/themes/{theme}/summary",
        )

    @task(1)
    def get_health(self):
        self.client.get("/health", name="/health")


class HeavyUser(HttpUser):
    """연속 조회 헤비 유저 시나리오 (전체 기간)."""
    wait_time = between(0.1, 0.5)
    weight = 1  # DashboardUser의 1/5 비율

    @task
    def full_range_regimes(self):
        self.client.get(
            "/leaders/regimes?start=2020-01-01&end=2026-05-29&limit=500",
            name="/leaders/regimes (full)",
        )

    @task
    def full_range_trades(self):
        self.client.get(
            "/trades?start=2020-01-01&end=2026-05-29&limit=1000",
            name="/trades (full)",
        )
