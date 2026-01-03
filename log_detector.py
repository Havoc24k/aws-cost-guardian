"""
Log-Based Cost Anomaly Detector
Monitors CloudWatch Logs for patterns that indicate cost-impacting events.
Complements metric-based detection with log analysis.
"""

import boto3
import re
import json
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Pattern
from decimal import Decimal
import logging

logger = logging.getLogger(__name__)


@dataclass
class LogCostRule:
    """Rule for detecting cost-impacting log patterns."""
    rule_id: str
    log_group: str
    pattern: str  # CloudWatch Logs Insights query pattern
    lookback_minutes: int
    cost_per_occurrence: Decimal
    threshold: Decimal
    remediation_action: str
    remediation_params: dict
    

class LogCostDetector:
    """Analyzes CloudWatch Logs for cost-impacting patterns."""
    
    def __init__(self, region: str = None):
        self.logs = boto3.client('logs', region_name=region)
    
    def query_logs(self, log_group: str, query: str, lookback_minutes: int) -> list[dict]:
        """Execute CloudWatch Logs Insights query."""
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=lookback_minutes)
        
        # Start query
        response = self.logs.start_query(
            logGroupName=log_group,
            startTime=int(start_time.timestamp() * 1000),
            endTime=int(end_time.timestamp() * 1000),
            queryString=query
        )
        
        query_id = response['queryId']
        
        # Poll for results
        while True:
            result = self.logs.get_query_results(queryId=query_id)
            
            if result['status'] in ['Complete', 'Failed', 'Cancelled']:
                break
        
        if result['status'] != 'Complete':
            logger.error(f"Query failed with status: {result['status']}")
            return []
        
        return result['results']
    
    def count_pattern_occurrences(self, log_group: str, pattern: str, lookback_minutes: int) -> int:
        """Count occurrences of a pattern in logs."""
        query = f"""
        fields @timestamp, @message
        | filter @message like /{pattern}/
        | stats count() as count
        """
        
        results = self.query_logs(log_group, query, lookback_minutes)
        
        if results and results[0]:
            for field in results[0]:
                if field['field'] == 'count':
                    return int(field['value'])
        
        return 0
    
    def evaluate_rule(self, rule: LogCostRule) -> dict:
        """Evaluate a single log-based cost rule."""
        count = self.count_pattern_occurrences(
            rule.log_group,
            rule.pattern,
            rule.lookback_minutes
        )
        
        projected_cost = Decimal(str(count)) * rule.cost_per_occurrence
        breach = projected_cost > rule.threshold
        
        return {
            'rule_id': rule.rule_id,
            'pattern_count': count,
            'projected_cost': str(projected_cost),
            'threshold': str(rule.threshold),
            'breach': breach
        }


# Common log patterns for cost-impacting events
LOG_PATTERNS = {
    'lambda_timeout': {
        'pattern': r'Task timed out after',
        'description': 'Lambda timeout - indicates wasted compute',
        'cost_multiplier': 1.0  # Full invocation cost lost
    },
    'lambda_memory_exceeded': {
        'pattern': r'Runtime exited with error.*memory',
        'description': 'Lambda OOM - indicates undersized memory',
        'cost_multiplier': 1.0
    },
    'retry_exhausted': {
        'pattern': r'Retry limit exceeded|Max retries reached',
        'description': 'Retries exhausted - indicates cascading failures',
        'cost_multiplier': 3.0  # Assume 3 retries
    },
    'throttling': {
        'pattern': r'ThrottlingException|Rate exceeded|Too Many Requests',
        'description': 'Throttling detected - may indicate runaway',
        'cost_multiplier': 0.5  # Partial cost due to failed requests
    },
    'cold_start': {
        'pattern': r'INIT_START|Cold start',
        'description': 'Cold start detected - additional latency cost',
        'cost_multiplier': 0.2  # Cold start overhead
    },
    'dynamodb_throttle': {
        'pattern': r'ProvisionedThroughputExceededException',
        'description': 'DynamoDB throttling - capacity issue',
        'cost_multiplier': 0.5
    },
    's3_slow_request': {
        'pattern': r'SlowDown|ServiceUnavailable.*S3',
        'description': 'S3 throttling - request rate issue',
        'cost_multiplier': 0.3
    },
    'connection_timeout': {
        'pattern': r'Connection timed out|connect ETIMEDOUT',
        'description': 'Connection timeout - network cost waste',
        'cost_multiplier': 0.8
    }
}


def create_log_rules_from_patterns(
    log_group: str,
    base_cost: Decimal,
    threshold: Decimal,
    remediation_action: str,
    remediation_params: dict,
    lookback_minutes: int = 5
) -> list[LogCostRule]:
    """Generate LogCostRules from predefined patterns."""
    rules = []
    
    for pattern_name, pattern_config in LOG_PATTERNS.items():
        rule = LogCostRule(
            rule_id=f"log-{pattern_name}",
            log_group=log_group,
            pattern=pattern_config['pattern'],
            lookback_minutes=lookback_minutes,
            cost_per_occurrence=base_cost * Decimal(str(pattern_config['cost_multiplier'])),
            threshold=threshold,
            remediation_action=remediation_action,
            remediation_params=remediation_params
        )
        rules.append(rule)
    
    return rules


class CombinedCostMonitor:
    """
    Combines metric-based and log-based cost detection.
    Provides unified evaluation interface.
    """
    
    def __init__(self, region: str = None):
        from cost_engine import CostGuardian
        self.metric_guardian = CostGuardian(region)
        self.log_detector = LogCostDetector(region)
        self.log_rules: list[LogCostRule] = []
    
    def add_metric_rule(self, rule):
        """Add a metric-based cost rule."""
        self.metric_guardian.add_rule(rule)
    
    def add_log_rule(self, rule: LogCostRule):
        """Add a log-based cost rule."""
        self.log_rules.append(rule)
    
    def evaluate_all(self, dry_run: bool = False) -> dict:
        """Evaluate all metric and log rules."""
        metric_results = self.metric_guardian.evaluate(dry_run=dry_run)
        
        log_results = []
        for rule in self.log_rules:
            result = self.log_detector.evaluate_rule(rule)
            log_results.append(result)
        
        return {
            'metric_evaluations': metric_results,
            'log_evaluations': log_results,
            'total_metric_breaches': sum(1 for r in metric_results if r['projection']['breach']),
            'total_log_breaches': sum(1 for r in log_results if r['breach'])
        }
