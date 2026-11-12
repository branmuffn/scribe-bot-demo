-- M14 health history (Status page): one row every 5 minutes from the worker, 30 days kept. Numbers only.
CREATE TABLE machine_sample (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,      -- UTC ISO
    cpu_pct       REAL,               -- % busy (all cores)
    load1         REAL,               -- 1-minute load average
    mem_used_pct  REAL,               -- active + wired + compressed / total RAM
    mem_free_pct  REAL,               -- memory_pressure "free percentage" (green >= 50, red < 20)
    swap_mb       REAL,
    temp_c        REAL,               -- NULL when osx-cpu-temp isn't installed or can't read
    fan_rpm       INTEGER,            -- fastest fan; NULL when unreadable
    speed_limit   INTEGER,            -- pmset CPU_Speed_Limit; 100 = not slowed for heat
    wifi_dbm      INTEGER,            -- signal of the current network
    on_battery    INTEGER,            -- 1 / 0 / NULL unknown
    disk_used_pct REAL,
    smart_ok      INTEGER             -- 1 Verified / 0 not / NULL unknown
);
CREATE INDEX machine_sample_ts ON machine_sample(ts);
