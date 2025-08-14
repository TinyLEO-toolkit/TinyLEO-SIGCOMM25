# ISL dynamics analysis for non-uniform satellite constellations

import multiprocessing as mp
from functools import partial
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
    def __init__(self, data_file):
        self.satellites = {}  # {sat_id: SatelliteInfo}
        
        # Load satellite data and initialize visibility parameters
        self.supply_data = np.load(data_file, allow_pickle=True)
        param0 = self.supply_data[0][0]
        self.height = param0[0]  # Same altitude for all satellites
        self.max_distances = {
            1: self._calculate_max_visibility_distance(self.height)
        }
        
        self._load_satellites()
        print(f"Loaded {len(self.satellites)} satellites in total")
        print(f"Satellite height: {self.height} km")
        print(f"Max visibility distance: {self.max_distances[1]:.2f} km")

    class SatelliteInfo:
        def __init__(self, sat_id, lon_lat_data, shell=1):
            self.sat_id = sat_id
            self.lon_lat_data = lon_lat_data
            self.shell = shell
            self.positions = {}

    def _calculate_max_visibility_distance(self, height):
        """Compute max line-of-sight distance at given altitude (km)"""
        R_EARTH = 6371
        ATMOSPHERE_HEIGHT = 80
        R_ATMOSPHERE = R_EARTH + ATMOSPHERE_HEIGHT
        r = R_EARTH + height
        return 2 * math.sqrt(r**2 - R_ATMOSPHERE**2)

    def _load_satellites(self):
        """Load non-uniform satellite data from npy"""
        print("Loading satellites...")
        for idx, data in enumerate(self.supply_data):
            _, _, _, sat_location, _ = data
            sat_info = self.SatelliteInfo(
                sat_id=str(idx),
                lon_lat_data=sat_location
            )
            self.satellites[str(idx)] = sat_info

    def _get_cartesian_position(self, lon, lat):
        """Convert geodetic to Cartesian coordinates"""
        R_EARTH = 6371
        r = R_EARTH + self.height
        x = r * math.cos(lat) * math.cos(lon)
        y = r * math.cos(lat) * math.sin(lon)
        z = r * math.sin(lat)
        return (float(x), float(y), float(z))

    def propagate_positions(self, duration_minutes):
        """Propagate positions over time for all satellites"""
        print("Calculating satellite positions...")
        for i, (sat_id, sat_info) in enumerate(self.satellites.items(), 1):
            print(f"\rPropagating positions for satellite {i}/{len(self.satellites)}", end='')
            for t in range(duration_minutes):
                lon, lat = sat_info.lon_lat_data[t]
                position = self._get_cartesian_position(lon, lat)
                sat_info.positions[t] = position
        print("\nPosition propagation completed")

class SpatialIndex:
    """3D grid spatial index for neighbor search"""
    def __init__(self, positions, max_dist):
        self.positions = positions
        self.max_dist = max_dist
        self.grid_size = max_dist
        self.grid = defaultdict(list)
        for sat_id, pos in positions.items():
            grid_key = tuple(int(coord / self.grid_size) for coord in pos)
            self.grid[grid_key].append(sat_id)

    def get_potential_neighbors(self, sat_id):
        """Return candidate neighbors in adjacent cells"""
        pos = self.positions[sat_id]
        grid_coord = tuple(int(coord / self.grid_size) for coord in pos)
        neighbors = set()
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                for dz in [-1, 0, 1]:
                    key = (grid_coord[0] + dx, grid_coord[1] + dy, grid_coord[2] + dz)
                    neighbors.update(self.grid[key])
        return neighbors - {sat_id}

def calculate_distance(pos1, pos2):
    """Compute Euclidean distance"""
    return np.sqrt(sum((a - b) ** 2 for a, b in zip(pos1, pos2)))

def calculate_shortest_path(source, target, visible_pairs, all_satellites, positions, max_distance):
    """Dijkstra-like path finding using visible links"""
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
    """Evaluate visibility between a pair of satellites"""
    sat_pair, positions, max_dist = args
    sat1_id, sat2_id = sat_pair
    if calculate_distance(positions[sat1_id], positions[sat2_id]) <= max_dist:
        return frozenset([sat1_id, sat2_id])
    return None

def get_visible_pairs(time, shell_sats, max_dist, network, pool):
    """Compute visible satellite pairs at given time"""
    positions = {sat_id: network.satellites[sat_id].positions[time] for sat_id in shell_sats}
    sat_pairs = [(s1, s2) for i, s1 in enumerate(shell_sats) for s2 in shell_sats[i+1:]]
    args = [(pair, positions, max_dist) for pair in sat_pairs]
    results = pool.map(process_satellite_pair, args)
    return set(pair for pair in results if pair is not None)

def process_satellite_visibility_and_paths(args):
    """Compute visible links and target paths for a single satellite"""
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
    """Main loop: analyze ISL breaks and path changes over time"""
    isl_breaks_over_time = []
    path_changes_over_time = []
    timestamps = []
    num_cores = mp.cpu_count()
    pool = mp.Pool(processes=num_cores)

    try:
        shell = 1
        print("\nAnalyzing network changes...")
        shell_satellites = list(network.satellites.keys())
        max_distance = network.max_distances[shell]

        print("Selecting target satellites for path analysis...")
        positions = {s: network.satellites[s].positions[0] for s in shell_satellites}
        spatial_index = SpatialIndex(positions, max_distance)

        target_satellites = {}
        for sat_id in tqdm(shell_satellites, desc="Selecting target satellites"):
            visible = {
                other_id for other_id in spatial_index.get_potential_neighbors(sat_id)
                if calculate_distance(positions[sat_id], positions[other_id]) <= max_distance
            }
            invisible = set(shell_satellites) - visible - {sat_id}
            target_satellites[sat_id] = random.sample(list(invisible), min(5, len(invisible)))

        print("\nProcessing initial state...")
        process_args = [(sat_id, shell_satellites, positions, max_distance, target_satellites, spatial_index) for sat_id in shell_satellites]
        results = pool.map(process_satellite_visibility_and_paths, process_args)

        prev_visible_pairs = set()
        prev_paths = {}
        for sid, vis, paths in results:
            prev_visible_pairs.update(vis)
            prev_paths.update(paths)

        pbar = tqdm(range(1, duration_minutes), desc="Processing network changes")
        for t in pbar:
            positions = {s: network.satellites[s].positions[t] for s in shell_satellites}
            spatial_index = SpatialIndex(positions, max_distance)
            process_args = [(sid, shell_satellites, positions, max_distance, target_satellites, spatial_index) for sid in shell_satellites]
            results = pool.map(process_satellite_visibility_and_paths, process_args)

            current_visible_pairs = set()
            current_paths = {}
            for sid, vis, paths in results:
                current_visible_pairs.update(vis)
                current_paths.update(paths)

            broken_isls = len(prev_visible_pairs - current_visible_pairs)
            path_changes = sum(
                1 for pair in prev_paths
                if pair in current_paths and prev_paths[pair] != current_paths[pair]
            )

            timestamps.append(t)
            isl_breaks_over_time.append(broken_isls)
            path_changes_over_time.append(path_changes)

            prev_visible_pairs = current_visible_pairs
            prev_paths = current_paths

            if t % 100 == 0:
                pbar.set_postfix({
                    'ISL breaks': broken_isls,
                    'Path changes': path_changes
                })

    finally:
        pool.close()
        pool.join()

    # Plot results
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 8))
    ax1.plot(timestamps, isl_breaks_over_time, linewidth=1)
    ax1.grid(True, linestyle='--', alpha=0.7)
    ax1.set_xlabel('Time step')
    ax1.set_ylabel('ISL Breaks')
    ax1.set_title('ISL Breaks Over Time')

    ax2.plot(timestamps, path_changes_over_time, linewidth=1)
    ax2.grid(True, linestyle='--', alpha=0.7)
    ax2.set_xlabel('Time step')
    ax2.set_ylabel('Path Changes')
    ax2.set_title('Shortest Path Changes Over Time')

    plt.tight_layout()
    plt.savefig('network_changes_over_time_nonuniform.png', dpi=300, bbox_inches='tight')
    plt.show()

    # Save results
    np.save('data/nonuniform_isls.npy', np.array(isl_breaks_over_time))
    np.save('data/nonuniform_paths.npy', np.array(path_changes_over_time))

    # Print statistics
    print("\nISL Breaks Statistics:")
    print(f"Max per step: {max(isl_breaks_over_time)}")
    print(f"Min per step: {min(isl_breaks_over_time)}")
    print(f"Average per step: {np.mean(isl_breaks_over_time):.2f}")
    
    print("\nPath Changes Statistics:")
    print(f"Max per step: {max(path_changes_over_time)}")
    print(f"Min per step: {min(path_changes_over_time)}")
    print(f"Average per step: {np.mean(path_changes_over_time):.2f}")

    return timestamps, isl_breaks_over_time, path_changes_over_time

def main():
    data_file = "data/nonuniform_constellation_satellite.npy"
    start_date = datetime(2024, 8, 21, 0, 0, 0, tzinfo=UTC)
    duration_minutes = 1438

    print("Loading satellite network...")
    network = SatelliteNetwork(data_file)

    print("Initializing satellite positions...")
    network.propagate_positions(duration_minutes)

    print("\nAnalyzing network changes...")
    timestamps, breaks, changes = analyze_network_changes(network, start_date, duration_minutes)
    print("Analysis completed")

if __name__ == "__main__":
    main()
