"""Print recent real database observations and enrichment, without fixture data."""
import json
from .storage import PostgresStorage


def main():
    store = PostgresStorage.from_env()
    try:
        with store.connection.cursor() as cursor:
            cursor.execute('SELECT now(), count(*), count(DISTINCT address) FROM aircraft_snapshots')
            at, snapshots, aircraft = cursor.fetchone()
            print(json.dumps({'queried_at': str(at), 'snapshots': snapshots, 'aircraft': aircraft}))
            cursor.execute('''SELECT bucket_start, address, frequency_hz, callsign,
                altitude_baro_ft, ground_speed_kt, latitude, longitude,
                registration, manufacturer, model, metadata_retrieved_at
                FROM enriched_snapshots_current ORDER BY bucket_start DESC LIMIT 12''')
            names = [column.name for column in cursor.description]
            for row in cursor.fetchall():
                print(json.dumps(dict(zip(names, row)), default=str))
            cursor.execute('''SELECT frequency_hz, count(*) FROM capture_intervals GROUP BY frequency_hz''')
            print('Capture turns:', cursor.fetchall())
    finally:
        store.connection.close()


if __name__ == '__main__':
    main()
