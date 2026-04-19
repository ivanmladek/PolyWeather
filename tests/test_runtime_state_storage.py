
import time

from src.database.runtime_state import (
    DailyRecordRepository,
    HFTemperatureRepository,
    OpenMeteoCacheRepository,
    ProbabilitySnapshotRepository,
    RuntimeStateDB,
    TelegramAlertStateRepository,
    TrainingFeatureRecordRepository,
    TruthRecordRepository,
    TruthRevisionRepository,
)


def test_daily_record_repository_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYWEATHER_DB_PATH', str(tmp_path / 'polyweather.db'))
    repo = DailyRecordRepository(RuntimeStateDB(str(tmp_path / 'polyweather.db')))
    repo.upsert_record('ankara', '2026-03-20', {'actual_high': 15.2, 'deb_prediction': 14.8, 'mu': 15.0})
    data = repo.load_all()
    assert data['ankara']['2026-03-20']['actual_high'] == 15.2


def test_telegram_alert_state_repository_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYWEATHER_DB_PATH', str(tmp_path / 'polyweather.db'))
    repo = TelegramAlertStateRepository(RuntimeStateDB(str(tmp_path / 'polyweather.db')))
    state = {
        'last_by_city': {
            'ankara': {
                'signature': 'sig-1',
                'trigger_key': 'mkt:test',
                'severity': 'medium',
                'ts': 123,
                'active': True,
                'evidence': {'x': 1},
            }
        },
        'by_signature': {'sig-1': 123},
    }
    repo.save_state(state)
    loaded = repo.load_state()
    assert loaded == state


def test_probability_snapshot_repository_recent_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYWEATHER_DB_PATH', str(tmp_path / 'polyweather.db'))
    repo = ProbabilitySnapshotRepository(RuntimeStateDB(str(tmp_path / 'polyweather.db')))
    repo.append_snapshot({
        'city': 'ankara',
        'date': '2026-03-20',
        'timestamp': '2026-03-20T12:00:00Z',
        'raw_mu': 15.2,
        'raw_sigma': 1.1,
        'max_so_far': 14.9,
        'peak_status': 'before',
        'probability_mode': 'emos_shadow',
        'prob_snapshot': [{'v': 15, 'p': 0.6}],
        'shadow_prob_snapshot': [{'v': 15, 'p': 0.4}],
    })
    rows = repo.load_recent_rows('ankara', '2026-03-20', 5)
    assert len(rows) == 1
    assert rows[0]['raw_mu'] == 15.2


def test_open_meteo_cache_repository_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYWEATHER_DB_PATH', str(tmp_path / 'polyweather.db'))
    repo = OpenMeteoCacheRepository(RuntimeStateDB(str(tmp_path / 'polyweather.db')))
    payload = {
        'forecast': {'ankara': {'t': time.time(), 'temp': 15}},
        'ensemble': {'ankara': {'t': time.time(), 'spread': 1.5}},
        'multi_model': {},
        'saved_at': 1000,
    }
    repo.replace_payload(payload, 86400)
    loaded = repo.load_payload(86400)
    assert loaded['forecast']['ankara']['temp'] == 15
    assert loaded['ensemble']['ankara']['spread'] == 1.5


def test_truth_record_repository_tracks_revisions(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYWEATHER_DB_PATH', str(tmp_path / 'polyweather.db'))
    db = RuntimeStateDB(str(tmp_path / 'polyweather.db'))
    truth_repo = TruthRecordRepository(db)
    revision_repo = TruthRevisionRepository(db)

    changed = truth_repo.upsert_truth(
        city='taipei',
        target_date='2026-04-01',
        actual_high=18.0,
        settlement_source='wunderground',
        settlement_station_code='RCSS',
        settlement_station_label='Taipei Songshan Airport Station',
        truth_version='v1',
        updated_by='test:first',
        source_payload={'raw_max_temp_c': 18.4},
        reason='initial',
    )
    assert changed is True
    assert revision_repo.load_revisions('taipei', '2026-04-01') == []

    changed = truth_repo.upsert_truth(
        city='taipei',
        target_date='2026-04-01',
        actual_high=19.0,
        settlement_source='wunderground',
        settlement_station_code='RCSS',
        settlement_station_label='Taipei Songshan Airport Station',
        truth_version='v1',
        updated_by='test:second',
        source_payload={'raw_max_temp_c': 19.1},
        reason='correction',
    )
    assert changed is True
    loaded = truth_repo.get_record('taipei', '2026-04-01')
    assert loaded['actual_high'] == 19.0
    revisions = revision_repo.load_revisions('taipei', '2026-04-01')
    assert len(revisions) == 1
    assert revisions[0]['previous_actual_high'] == 18.0
    assert revisions[0]['next_actual_high'] == 19.0


def test_training_feature_record_repository_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYWEATHER_DB_PATH', str(tmp_path / 'polyweather.db'))
    db = RuntimeStateDB(str(tmp_path / 'polyweather.db'))
    repo = TrainingFeatureRecordRepository(db)
    repo.upsert_record(
        'ankara',
        '2026-03-20',
        {
            'forecasts': {'ECMWF': 12.3},
            'deb_prediction': 12.1,
            'mu': 12.0,
            'probability_features': {'ens_median': 12.2},
        },
    )
    loaded = repo.get_record('ankara', '2026-03-20')
    assert loaded['forecasts']['ECMWF'] == 12.3
    assert loaded['deb_prediction'] == 12.1


def test_hf_temperature_repository_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv('POLYWEATHER_DB_PATH', str(tmp_path / 'polyweather.db'))
    db = RuntimeStateDB(str(tmp_path / 'polyweather.db'))
    repo = HFTemperatureRepository(db)

    observations = [
        {"utc_time": "2026-04-19T14:01:00Z", "local_time": "09:01", "temp_f": 78.1, "temp_c": 25.6, "dwp_f": 55.0, "dwp_c": 12.8},
        {"utc_time": "2026-04-19T14:02:00Z", "local_time": "09:02", "temp_f": 78.3, "temp_c": 25.7, "dwp_f": 55.1, "dwp_c": 12.8},
        {"utc_time": "2026-04-19T14:03:00Z", "local_time": "09:03", "temp_f": 78.5, "temp_c": 25.8, "dwp_f": 55.2, "dwp_c": 12.9},
    ]

    count = repo.bulk_upsert_observations(icao="KLGA", target_date="2026-04-19", observations=observations)
    assert count == 3

    loaded = repo.load_observations(icao="KLGA", target_date="2026-04-19")
    assert len(loaded) == 3
    assert loaded[0]["temp_f"] == 78.1
    assert loaded[2]["temp_f"] == 78.5

    # Test peak detection persistence
    peak_result = {
        "status": "post_peak",
        "confidence": 0.75,
        "peak_temp_f": 85.0,
        "peak_temp_c": 29.4,
        "peak_time": "14:30",
        "alpha_signal": "strong_post_peak",
        "alpha_minutes_ahead": 25,
        "observation_count": 120,
    }
    repo.upsert_peak_detection(icao="KLGA", target_date="2026-04-19", result_dict=peak_result)
    loaded_peak = repo.load_peak_detection(icao="KLGA", target_date="2026-04-19")
    assert loaded_peak is not None
    assert loaded_peak["status"] == "post_peak"
    assert loaded_peak["confidence"] == 0.75
    assert loaded_peak["alpha_signal"] == "strong_post_peak"

    # Test overwrite
    peak_result["confidence"] = 0.85
    repo.upsert_peak_detection(icao="KLGA", target_date="2026-04-19", result_dict=peak_result)
    loaded_peak2 = repo.load_peak_detection(icao="KLGA", target_date="2026-04-19")
    assert loaded_peak2["confidence"] == 0.85
