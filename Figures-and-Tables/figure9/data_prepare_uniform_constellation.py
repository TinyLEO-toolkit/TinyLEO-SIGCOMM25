# Uniform satellite constellation: ISL dynamics analysis

import multiprocessing as mp
from functools import partial
from skyfield.api import load, EarthSatellite
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import os
from collections import defaultdict
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
import pytz
from pytz import UTC
from tqdm import tqdm
import math
import random
from itertools import combinations

class SatelliteNetwork:
    def __init__(self, shell2_dir, shell3_dir, start_date):
        self.ts = load.timescale()
        self.satellites = {}  # {sat_id: SatelliteInfo}
        self.start_date = start_date

        # Precompute max visible distances for different shells
        self.max_distances = {
            2: self._calculate_max_visibility_distance(540),
            3: self._calculate_max_visibility_distance(570)
        }

        # Load satellite data
        self._load_satellites(shell2_dir, 2)
        self._load_satellites(shell3_dir, 3)

        print(f"Loaded {len(self.satellites)} satellites in total")
        print(f"Shell 2: {len([s for s in self.satellites.values() if s.shell == 2])} satellites")
        print(f"Shell 3: {len([s for s in self.satellites.values() if s.shell == 3])} satellites")

    class SatelliteInfo:
        def __init__(self, sat_id, tle1, tle2, shell):
            self.sat_id = sat_id
            self.tle1 = tle1
            self.tle2 = tle2
            self.shell = shell
            self.skyfield_sat = None
            self.positions = {}

    def _calculate_max_visibility_distance(self, height):
        """Compute max line-of-sight distance for given orbital altitude (km)"""
        R_EARTH = 6371
        ATMOSPHERE_HEIGHT = 80
        R_ATMOSPHERE = R_EARTH + ATMOSPHERE_HEIGHT
        r = R_EARTH + height
        return 2 * math.sqrt(r**2 - R_ATMOSPHERE**2)

    def _load_satellites(self, directory, shell_num):
        """Load satellite TLEs from CSV files"""
        print(f"Loading satellites from {directory} for shell {shell_num}")
        for filename in os.listdir(directory):
            if not filename.endswith('.csv'):
                continue
            sat_id = filename.split('.')[0]
            df = pd.read_csv(os.path.join(directory, filename))
            target_date = self.start_date.strftime('%Y-%m-%d')
            tle_data = df[df['EPOCH'].str.startswith(target_date)]
            if len(tle_data) == 0:
                continue
            tle_row = tle_data.iloc[0]
            self.satellites[sat_id] = self.SatelliteInfo(
                sat_id=sat_id,
                tle1=tle_row['TLE_LINE1'],
                tle2=tle_row['TLE_LINE2'],
                shell=shell_num
            )

    def propagate_positions(self, duration_minutes):
        """Propagate all satellites' positions over the given duration (min)"""
        utc = pytz.UTC
        time_points = []
        skyfield_times = []
        for i in range(duration_minutes):
            dt = self.start_date + timedelta(minutes=i)
            dt = dt.replace(tzinfo=utc)
            time_points.append(dt)
            skyfield_times.append(self.ts.from_datetime(dt))
        for i, (sat_id, sat_info) in enumerate(self.satellites.items(), 1):
            print(f"\rPropagating positions for satellite {i}/{len(self.satellites)}: {sat_id}", end='')
            sat = EarthSatellite(sat_info.tle1, sat_info.tle2, sat_id, self.ts)
            sat_info.skyfield_sat = sat
            for sf_time, dt in zip(skyfield_times, time_points):
                geocentric = sat.at(sf_time)
                position = geocentric.position.km
                sat_info.positions[dt] = tuple(float(p) for p in position)
        print("\nPosition propagation completed")

class SpatialIndex:
    """Spatial grid index for fast neighbor lookup"""
    def __init__(self, positions, max_dist):
        self.positions = positions
        self.max_dist = max_dist
        self.grid_size = max_dist
        self.grid = defaultdict(list)
        for sat_id, pos in positions.items():
            key = tuple(int(p / self.grid_size) for p in pos)
            self.grid[key].append(sat_id)

    def get_potential_neighbors(self, sat_id):
        """Return nearby satellite IDs in adjacent grid cells"""
        pos = self.positions[sat_id]
        grid_coord = tuple(int(p / self.grid_size) for p in pos)
        neighbors = set()
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                for dz in [-1, 0, 1]:
                    key = (grid_coord[0]+dx, grid_coord[1]+dy, grid_coord[2]+dz)
                    neighbors.update(self.grid[key])
        return neighbors - {sat_id}

def calculate_distance(pos1, pos2):
    """Euclidean distance"""
    return np.sqrt(sum((a - b) ** 2 for a, b in zip(pos1, pos2)))

def calculate_shortest_path(source, target, visible_pairs, all_satellites, positions, max_distance):
    """Optimized Dijkstra's algorithm (hop-based)"""
    from heapq import heappush, heappop
    pq = [(0, source, [source])]
    visited = set()
    while pq:
        dist, current, path = heappop(pq)
        if current == target:
            return path if len(path) >= 3 else None
        if current in visited:
            continue
        visited.add(current)
        for neighbor in all_satellites:
            if neighbor not in visited and frozenset([current, neighbor]) in visible_pairs:
                heappush(pq, (dist + 1, neighbor, path + [neighbor]))
    return None

def process_satellite_pair(args):
    """Check visibility for a satellite pair"""
    (sat1_id, sat2_id), positions, max_dist = args
    if calculate_distance(positions[sat1_id], positions[sat2_id]) <= max_dist:
        return frozenset([sat1_id, sat2_id])
    return None

def get_visible_pairs(time, shell_sats, max_dist, network, pool):
    """Return all visible satellite pairs at given time"""
    positions = {s: network.satellites[s].positions[time] for s in shell_sats}
    sat_pairs = [(s1, s2) for i, s1 in enumerate(shell_sats) for s2 in shell_sats[i+1:]]
    args = [(pair, positions, max_dist) for pair in sat_pairs]
    results = pool.map(process_satellite_pair, args)
    return set(pair for pair in results if pair is not None)

def process_satellite_visibility_and_paths(args):
    """Compute visible links and shortest paths for one satellite"""
    source_id, all_satellites, positions, max_dist, target_satellites, spatial_index = args
    potential_neighbors = spatial_index.get_potential_neighbors(source_id)
    visible_sats = set()
    for other_id in potential_neighbors:
        if calculate_distance(positions[source_id], positions[other_id]) <= max_dist:
            visible_sats.add(frozenset([source_id, other_id]))
    paths = {}
    for target_id in target_satellites.get(source_id, []):
        path = calculate_shortest_path(source_id, target_id, visible_sats, all_satellites, positions, max_dist)
        if path:
            paths[frozenset([source_id, target_id])] = path
    return source_id, visible_sats, paths

def analyze_network_changes(network, initial_time, duration_minutes):
    """Main analysis: ISL breaks and path changes over time"""
    isl_breaks_over_time = []
    path_changes_over_time = []
    timestamps = []
    num_cores = mp.cpu_count()
    pool = mp.Pool(processes=num_cores)

    try:
        for shell in [2, 3]:
            print(f"\nAnalyzing Shell {shell} network changes...")
            shell_satellites = [s for s, info in network.satellites.items() if info.shell == shell]
            max_distance = network.max_distances[shell]
            target_satellites = {}
            if shell == 2:
                print("Selecting path targets...")
                positions = {s: network.satellites[s].positions[initial_time] for s in shell_satellites}
                spatial_index = SpatialIndex(positions, max_distance)
                for sat_id in shell_satellites:
                    potential = spatial_index.get_potential_neighbors(sat_id)
                    visible = {o for o in potential if calculate_distance(positions[sat_id], positions[o]) <= max_distance}
                    invisible = set(shell_satellites) - visible - {sat_id}
                    target_satellites[sat_id] = random.sample(list(invisible), min(10, len(invisible)))

            print("Processing initial state...")
            positions = {s: network.satellites[s].positions[initial_time] for s in shell_satellites}
            spatial_index = SpatialIndex(positions, max_distance)
            args = [(s, shell_satellites, positions, max_distance, target_satellites, spatial_index) for s in shell_satellites]
            results = pool.map(process_satellite_visibility_and_paths, args)

            prev_visible_pairs = set()
            prev_paths = {}
            for sid, vis, paths in results:
                prev_visible_pairs.update(vis)
                prev_paths.update(paths)

            print("Processing time series...")
            for minute in tqdm(range(1, duration_minutes)):
                current_time = initial_time + timedelta(minutes=minute)
                positions = {s: network.satellites[s].positions[current_time] for s in shell_satellites}
                spatial_index = SpatialIndex(positions, max_distance)
                args = [(s, shell_satellites, positions, max_distance, target_satellites, spatial_index) for s in shell_satellites]
                results = pool.map(process_satellite_visibility_and_paths, args)

                current_visible_pairs = set()
                current_paths = {}
                for sid, vis, paths in results:
                    current_visible_pairs.update(vis)
                    current_paths.update(paths)

                broken_isls = len(prev_visible_pairs - current_visible_pairs)
                path_changes = sum(1 for p in prev_paths if p in current_paths and prev_paths[p] != current_paths[p])
                if shell == 2:
                    timestamps.append(minute)
                    isl_breaks_over_time.append(broken_isls)
                    path_changes_over_time.append(path_changes)
                else:
                    isl_breaks_over_time[minute - 1] += broken_isls
                    path_changes_over_time[minute - 1] += path_changes

                prev_visible_pairs = current_visible_pairs
                prev_paths = current_paths
    finally:
        pool.close()
        pool.join()

    # Save data
    np.save('uniform_isls.npy', np.array(isl_breaks_over_time))
    np.save('uniform_paths.npy', np.array(path_changes_over_time))

    # Print stats
    print("\nISL Breaks Statistics:")
    print(f"Max per minute: {max(isl_breaks_over_time)}")
    print(f"Min per minute: {min(isl_breaks_over_time)}")
    print(f"Average per minute: {np.mean(isl_breaks_over_time):.2f}")
    print("\nPath Changes Statistics:")
    print(f"Max per minute: {max(path_changes_over_time)}")
    print(f"Min per minute: {min(path_changes_over_time)}")
    print(f"Average per minute: {np.mean(path_changes_over_time):.2f}")

    return timestamps, isl_breaks_over_time, path_changes_over_time

def main():
    shell2_dir = "data/shell2_TLE"
    shell3_dir = "data/shell3_TLE"
    start_date = datetime(2024, 8, 21, 0, 0, 0, tzinfo=UTC)
    duration_minutes = 1438  # 24 hours
    network = SatelliteNetwork(shell2_dir, shell3_dir, start_date)
    network.propagate_positions(duration_minutes)
    print("\nAnalyzing ISL breaks...")
    timestamps, breaks, path_changes = analyze_network_changes(network, start_date, duration_minutes)

if __name__ == "__main__":
    main()
