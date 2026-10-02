#!/usr/bin/env python3
"""
qBittorrent Prometheus Exporter

Exports torrent metrics as proper Prometheus gauges with low cardinality.
- Uses torrent hash as the unique identifier (stable)
- Stores uploaded/downloaded as gauge values, not labels
- Supports delta calculations via Prometheus offset queries
"""

import os
import time
import hashlib
import logging
from typing import Dict, Any, Optional
from prometheus_client import start_http_server, Gauge, Info, Counter, REGISTRY
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily
import requests

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration from environment
QBIT_HOST = os.getenv('QBIT_HOST', 'localhost')
QBIT_PORT = int(os.getenv('QBIT_PORT', '8080'))
QBIT_USERNAME = os.getenv('QBIT_USERNAME', 'admin')
QBIT_PASSWORD = os.getenv('QBIT_PASSWORD', 'adminadmin')
EXPORTER_PORT = int(os.getenv('EXPORTER_PORT', '9100'))
SCRAPE_INTERVAL = int(os.getenv('SCRAPE_INTERVAL', '60'))

# qBittorrent API base URL
QBIT_API_URL = f"http://{QBIT_HOST}:{QBIT_PORT}/api/v2"


class QBittorrentCollector:
    """Custom collector for qBittorrent metrics"""
    
    def __init__(self):
        self.session = requests.Session()
        self.authenticated = False
        self._last_auth_attempt = 0
        self._auth_cooldown = 30  # seconds between auth attempts
        
    def _authenticate(self) -> bool:
        """Authenticate with qBittorrent WebUI"""
        now = time.time()
        if now - self._last_auth_attempt < self._auth_cooldown:
            return self.authenticated
            
        self._last_auth_attempt = now
        
        try:
            response = self.session.post(
                f"{QBIT_API_URL}/auth/login",
                data={'username': QBIT_USERNAME, 'password': QBIT_PASSWORD},
                timeout=10
            )
            if response.text == 'Ok.':
                self.authenticated = True
                logger.info("Successfully authenticated with qBittorrent")
                return True
            else:
                logger.warning(f"Authentication failed: {response.text}")
                self.authenticated = False
                return False
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            self.authenticated = False
            return False
    
    def _get_torrents(self) -> Optional[list]:
        """Fetch torrent list from qBittorrent API"""
        if not self.authenticated:
            if not self._authenticate():
                return None
        
        try:
            response = self.session.get(
                f"{QBIT_API_URL}/torrents/info",
                timeout=30
            )
            if response.status_code == 403:
                self.authenticated = False
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching torrents: {e}")
            return None
    
    def _get_app_version(self) -> Optional[str]:
        """Fetch qBittorrent version"""
        if not self.authenticated:
            if not self._authenticate():
                return None
        
        try:
            response = self.session.get(
                f"{QBIT_API_URL}/app/version",
                timeout=10
            )
            return response.text.strip()
        except Exception as e:
            logger.error(f"Error fetching version: {e}")
            return None
    
    def _get_transfer_info(self) -> Optional[dict]:
        """Fetch global transfer info"""
        if not self.authenticated:
            if not self._authenticate():
                return None
        
        try:
            response = self.session.get(
                f"{QBIT_API_URL}/transfer/info",
                timeout=10
            )
            return response.json()
        except Exception as e:
            logger.error(f"Error fetching transfer info: {e}")
            return None
    
    def _clean_tracker(self, tracker_url: str) -> str:
        """Extract clean tracker hostname from URL"""
        if not tracker_url:
            return "unknown"
        try:
            # Remove protocol
            tracker = tracker_url.replace("https://", "").replace("http://", "")
            # Get hostname
            tracker = tracker.split("/")[0].split(":")[0]
            return tracker
        except:
            return "unknown"
    
    def _get_short_hash(self, full_hash: str) -> str:
        """Get first 8 chars of hash for display"""
        return full_hash[:8] if full_hash else "unknown"
    
    def collect(self):
        """Collect metrics from qBittorrent"""
        
        # App version info
        version = self._get_app_version()
        if version:
            info = GaugeMetricFamily(
                'qbittorrent_app_version',
                'qBittorrent application version',
                labels=['version']
            )
            info.add_metric([version], 1)
            yield info
        
        # Global transfer info
        transfer = self._get_transfer_info()
        if transfer:
            # Global download speed
            dl_speed = GaugeMetricFamily(
                'qbittorrent_global_download_speed_bytes',
                'Global download speed in bytes/s'
            )
            dl_speed.add_metric([], transfer.get('dl_info_speed', 0))
            yield dl_speed
            
            # Global upload speed
            ul_speed = GaugeMetricFamily(
                'qbittorrent_global_upload_speed_bytes',
                'Global upload speed in bytes/s'
            )
            ul_speed.add_metric([], transfer.get('up_info_speed', 0))
            yield ul_speed
            
            # Session downloaded
            session_dl = GaugeMetricFamily(
                'qbittorrent_session_downloaded_bytes',
                'Total bytes downloaded this session'
            )
            session_dl.add_metric([], transfer.get('dl_info_data', 0))
            yield session_dl
            
            # Session uploaded
            session_ul = GaugeMetricFamily(
                'qbittorrent_session_uploaded_bytes',
                'Total bytes uploaded this session'
            )
            session_ul.add_metric([], transfer.get('up_info_data', 0))
            yield session_ul
        
        # Torrent metrics
        torrents = self._get_torrents()
        if not torrents:
            # Yield up metric as 0 to indicate exporter is down
            up = GaugeMetricFamily('qbittorrent_up', 'qBittorrent exporter is up')
            up.add_metric([], 0)
            yield up
            return
        
        # Exporter is up
        up = GaugeMetricFamily('qbittorrent_up', 'qBittorrent exporter is up')
        up.add_metric([], 1)
        yield up
        
        # Total torrent count
        total = GaugeMetricFamily('qbittorrent_torrents_total', 'Total number of torrents')
        total.add_metric([], len(torrents))
        yield total
        
        # Per-torrent metrics with LOW CARDINALITY labels
        # Using hash (stable) + tracker (useful for grouping)
        
        uploaded = GaugeMetricFamily(
            'qbittorrent_torrent_uploaded_bytes',
            'Total bytes uploaded for torrent',
            labels=['hash', 'tracker', 'category']
        )
        
        downloaded = GaugeMetricFamily(
            'qbittorrent_torrent_downloaded_bytes',
            'Total bytes downloaded for torrent',
            labels=['hash', 'tracker', 'category']
        )
        
        size = GaugeMetricFamily(
            'qbittorrent_torrent_size_bytes',
            'Total size of torrent in bytes',
            labels=['hash', 'tracker', 'category']
        )
        
        ratio = GaugeMetricFamily(
            'qbittorrent_torrent_ratio',
            'Upload/download ratio for torrent',
            labels=['hash', 'tracker', 'category']
        )
        
        progress = GaugeMetricFamily(
            'qbittorrent_torrent_progress',
            'Download progress (0-1)',
            labels=['hash', 'tracker', 'category']
        )
        
        dl_speed = GaugeMetricFamily(
            'qbittorrent_torrent_download_speed_bytes',
            'Current download speed in bytes/s',
            labels=['hash', 'tracker', 'category']
        )
        
        ul_speed = GaugeMetricFamily(
            'qbittorrent_torrent_upload_speed_bytes',
            'Current upload speed in bytes/s',
            labels=['hash', 'tracker', 'category']
        )
        
        seeders = GaugeMetricFamily(
            'qbittorrent_torrent_seeders',
            'Number of seeders',
            labels=['hash', 'tracker', 'category']
        )
        
        leechers = GaugeMetricFamily(
            'qbittorrent_torrent_leechers',
            'Number of leechers',
            labels=['hash', 'tracker', 'category']
        )
        
        time_active = GaugeMetricFamily(
            'qbittorrent_torrent_time_active_seconds',
            'Time torrent has been active',
            labels=['hash', 'tracker', 'category']
        )
        
        added_on = GaugeMetricFamily(
            'qbittorrent_torrent_added_timestamp',
            'Timestamp when torrent was added',
            labels=['hash', 'tracker', 'category']
        )
        
        # Torrent info metric - for name lookup (separate, lower frequency)
        torrent_info = GaugeMetricFamily(
            'qbittorrent_torrent_info',
            'Torrent information (value is always 1)',
            labels=['hash', 'name', 'state', 'tracker', 'category']
        )
        
        # Aggregate metrics by tracker
        tracker_stats: Dict[str, Dict[str, float]] = {}
        state_counts: Dict[str, int] = {}
        category_counts: Dict[str, int] = {}
        
        for t in torrents:
            torrent_hash = t.get('hash', 'unknown')
            tracker = self._clean_tracker(t.get('tracker', ''))
            category = t.get('category', '') or 'uncategorized'
            state = t.get('state', 'unknown')
            name = t.get('name', 'unknown')
            
            labels = [torrent_hash, tracker, category]
            
            # Per-torrent gauge metrics
            uploaded.add_metric(labels, t.get('uploaded', 0))
            downloaded.add_metric(labels, t.get('downloaded', 0))
            size.add_metric(labels, t.get('size', 0))
            ratio.add_metric(labels, t.get('ratio', 0))
            progress.add_metric(labels, t.get('progress', 0))
            dl_speed.add_metric(labels, t.get('dlspeed', 0))
            ul_speed.add_metric(labels, t.get('upspeed', 0))
            seeders.add_metric(labels, t.get('num_seeds', 0))
            leechers.add_metric(labels, t.get('num_leechs', 0))
            time_active.add_metric(labels, t.get('time_active', 0))
            added_on.add_metric(labels, t.get('added_on', 0))
            
            # Info metric with name (for joining in Grafana)
            torrent_info.add_metric([torrent_hash, name, state, tracker, category], 1)
            
            # Aggregate by tracker
            if tracker not in tracker_stats:
                tracker_stats[tracker] = {'uploaded': 0, 'downloaded': 0, 'count': 0}
            tracker_stats[tracker]['uploaded'] += t.get('uploaded', 0)
            tracker_stats[tracker]['downloaded'] += t.get('downloaded', 0)
            tracker_stats[tracker]['count'] += 1
            
            # Count states
            state_counts[state] = state_counts.get(state, 0) + 1
            
            # Count categories
            category_counts[category] = category_counts.get(category, 0) + 1
        
        yield uploaded
        yield downloaded
        yield size
        yield ratio
        yield progress
        yield dl_speed
        yield ul_speed
        yield seeders
        yield leechers
        yield time_active
        yield added_on
        yield torrent_info
        
        # Tracker aggregate metrics
        tracker_uploaded = GaugeMetricFamily(
            'qbittorrent_tracker_uploaded_bytes_total',
            'Total bytes uploaded per tracker',
            labels=['tracker']
        )
        tracker_downloaded = GaugeMetricFamily(
            'qbittorrent_tracker_downloaded_bytes_total',
            'Total bytes downloaded per tracker',
            labels=['tracker']
        )
        tracker_count = GaugeMetricFamily(
            'qbittorrent_tracker_torrents_total',
            'Number of torrents per tracker',
            labels=['tracker']
        )
        
        for tracker, stats in tracker_stats.items():
            tracker_uploaded.add_metric([tracker], stats['uploaded'])
            tracker_downloaded.add_metric([tracker], stats['downloaded'])
            tracker_count.add_metric([tracker], stats['count'])
        
        yield tracker_uploaded
        yield tracker_downloaded
        yield tracker_count
        
        # State counts
        state_metric = GaugeMetricFamily(
            'qbittorrent_torrents_by_state',
            'Number of torrents by state',
            labels=['state']
        )
        for state, count in state_counts.items():
            state_metric.add_metric([state], count)
        yield state_metric
        
        # Category counts
        category_metric = GaugeMetricFamily(
            'qbittorrent_torrents_by_category',
            'Number of torrents by category',
            labels=['category']
        )
        for category, count in category_counts.items():
            category_metric.add_metric([category], count)
        yield category_metric


def main():
    """Main entry point"""
    logger.info(f"Starting qBittorrent exporter on port {EXPORTER_PORT}")
    logger.info(f"Connecting to qBittorrent at {QBIT_HOST}:{QBIT_PORT}")
    
    # Register custom collector
    collector = QBittorrentCollector()
    REGISTRY.register(collector)
    
    # Start HTTP server
    start_http_server(EXPORTER_PORT)
    logger.info(f"Exporter running at http://0.0.0.0:{EXPORTER_PORT}/metrics")
    
    # Keep running
    while True:
        time.sleep(60)


if __name__ == '__main__':
    main()
