"""
AWS Lambda Handler for Cost Guardian
Triggered by CloudWatch Events on a schedule (e.g., every 1 minute).
"""

import os
import json
import boto3
import logging
from cost_engine import CostGuardian, CostRule
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Configuration sources
CONFIG_S3_BUCKET = os.environ.get('CONFIG_S3_BUCKET')
CONFIG_S3_KEY = os.environ.get('CONFIG_S3_KEY', 'cost-guardian/rules.json')
CONFIG_SSM_PARAMETER = os.environ.get('CONFIG_SSM_PARAMETER')
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'
REGION = os.environ.get('AWS_REGION', 'us-east-1')


def load_config_from_s3() -> dict:
    """Load rule configuration from S3."""
    s3 = boto3.client('s3')
    response = s3.get_object(Bucket=CONFIG_S3_BUCKET, Key=CONFIG_S3_KEY)
    return json.loads(response['Body'].read().decode('utf-8'))


def load_config_from_ssm() -> dict:
    """Load rule configuration from SSM Parameter Store."""
    ssm = boto3.client('ssm')
    response = ssm.get_parameter(Name=CONFIG_SSM_PARAMETER, WithDecryption=True)
    return json.loads(response['Parameter']['Value'])


def load_config_from_env() -> dict:
    """
    Load rule configuration from environment variable.
    Useful for simple deployments or testing.
    """
    config_json = os.environ.get('COST_GUARDIAN_CONFIG')
    if config_json:
        return json.loads(config_json)
    return None


def load_config() -> dict:
    """Load configuration from available source."""
    # Priority: Environment > SSM > S3
    config = load_config_from_env()
    if config:
        logger.info("Loaded config from environment variable")
        return config
    
    if CONFIG_SSM_PARAMETER:
        config = load_config_from_ssm()
        logger.info(f"Loaded config from SSM: {CONFIG_SSM_PARAMETER}")
        return config
    
    if CONFIG_S3_BUCKET:
        config = load_config_from_s3()
        logger.info(f"Loaded config from S3: {CONFIG_S3_BUCKET}/{CONFIG_S3_KEY}")
        return config
    
    raise ValueError("No configuration source specified. Set CONFIG_S3_BUCKET, CONFIG_SSM_PARAMETER, or COST_GUARDIAN_CONFIG")


def handler(event, context):
    """
    Lambda entry point.
    
    Event can optionally include:
    - dry_run: bool - Override DRY_RUN env var
    - rules: list - Override rules from config (for testing)
    """
    logger.info(f"Cost Guardian triggered. Event: {json.dumps(event)}")
    
    dry_run = event.get('dry_run', DRY_RUN)
    
    # Initialize guardian
    guardian = CostGuardian(region=REGION)
    
    # Load rules
    if 'rules' in event:
        # Rules provided directly in event (useful for testing)
        guardian.load_rules_from_config({'rules': event['rules']})
    else:
        config = load_config()
        guardian.load_rules_from_config(config)
    
    logger.info(f"Loaded {len(guardian.rules)} rules. Dry run: {dry_run}")
    
    # Evaluate all rules
    results = guardian.evaluate(dry_run=dry_run)
    
    # Log results
    breaches = [r for r in results if r['projection']['breach']]
    logger.info(f"Evaluation complete. {len(breaches)} breaches out of {len(results)} rules")
    
    for result in results:
        if result['projection']['breach']:
            logger.warning(f"BREACH: {result['rule_id']} - Projected: ${result['projection']['projected_cost']} > Threshold: ${result['projection']['threshold']}")
            if result['remediation']:
                logger.info(f"Remediation executed: {result['remediation']}")
    
    return {
        'statusCode': 200,
        'body': {
            'evaluated': len(results),
            'breaches': len(breaches),
            'dry_run': dry_run,
            'results': results
        }
    }


def test_handler():
    """Local test function."""
    test_event = {
        'dry_run': True,
        'rules': [
            {
                'rule_id': 'test-lambda-cost',
                'metric_namespace': 'AWS/Lambda',
                'metric_name': 'Invocations',
                'dimensions': [
                    {'Name': 'FunctionName', 'Value': 'my-function'}
                ],
                'lookback_seconds': 120,
                'projection_seconds': 600,
                'unit_cost': '0.0000002',
                'threshold': '1.00',
                'remediation_action': 'notify_sns',
                'remediation_params': {
                    'topic_arn': 'arn:aws:sns:us-east-1:123456789012:cost-alerts'
                }
            }
        ]
    }
    
    result = handler(test_event, None)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    test_handler()
