#!/usr/bin/env python3
"""
Cost Guardian CLI
Test and validate cost rules locally before deployment.
"""

import argparse
import json
import sys
from decimal import Decimal
from datetime import datetime, timezone

# Local imports
from cost_engine import CostGuardian, CostRule, CostEngine


def cmd_evaluate(args):
    """Evaluate rules against live CloudWatch metrics."""
    guardian = CostGuardian(region=args.region)
    
    with open(args.config, 'r') as f:
        config = json.load(f)
    
    guardian.load_rules_from_config(config)
    
    print(f"Evaluating {len(guardian.rules)} rules (dry_run={args.dry_run})...\n")
    
    results = guardian.evaluate(dry_run=args.dry_run)
    
    for result in results:
        status = "BREACH" if result['projection']['breach'] else "OK"
        print(f"[{status}] {result['rule_id']}")
        print(f"    Rate: {result['projection']['rate_per_second']:.4f}/sec")
        print(f"    Projected Cost: ${result['projection']['projected_cost']}")
        print(f"    Threshold: ${result['projection']['threshold']}")
        
        if result['remediation']:
            print(f"    Remediation: {result['remediation']}")
        print()
    
    breaches = sum(1 for r in results if r['projection']['breach'])
    print(f"Total: {breaches} breaches out of {len(results)} rules")
    
    return 0 if breaches == 0 else 1


def cmd_simulate(args):
    """Simulate cost projection with synthetic values."""
    lookback = args.lookback
    projection = args.projection
    rate = args.rate
    unit_cost = Decimal(args.unit_cost)
    threshold = Decimal(args.threshold)
    
    total_in_lookback = rate * lookback
    rate_per_second = total_in_lookback / lookback
    projected_units = rate_per_second * projection
    projected_cost = Decimal(str(projected_units)) * unit_cost
    
    breach = projected_cost > threshold
    
    print("Simulation Results:")
    print(f"  Lookback period: {lookback}s")
    print(f"  Projection period: {projection}s")
    print(f"  Input rate: {rate}/sec")
    print(f"  Unit cost: ${unit_cost}")
    print(f"  Threshold: ${threshold}")
    print()
    print(f"  Total in lookback: {total_in_lookback}")
    print(f"  Calculated rate: {rate_per_second}/sec")
    print(f"  Projected units: {projected_units}")
    print(f"  Projected cost: ${projected_cost:.6f}")
    print(f"  Breach: {breach}")
    
    return 0


def cmd_get_pricing(args):
    """Fetch current AWS pricing for a service."""
    engine = CostEngine(region=args.region)
    
    if args.service == 'lambda':
        cost = engine.get_lambda_invocation_cost(args.region)
        print(f"Lambda invocation cost ({args.region}): ${cost} per invocation")
        print(f"  Per 1,000 invocations: ${cost * 1000}")
        print(f"  Per 1,000,000 invocations: ${cost * 1000000}")
    else:
        print(f"Pricing lookup not implemented for: {args.service}")
        return 1
    
    return 0


def cmd_validate(args):
    """Validate a rules configuration file."""
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}")
        return 1
    
    if 'rules' not in config:
        print("Missing 'rules' key in configuration")
        return 1
    
    errors = []
    warnings = []

    # Base required fields for all rules
    base_required_fields = [
        'rule_id', 'metric_namespace', 'metric_name',
        'lookback_seconds', 'projection_seconds',
        'threshold', 'remediation_action'
    ]

    for i, rule in enumerate(config['rules']):
        rule_id = rule.get('rule_id', f'rule_{i}')
        pricing_model = rule.get('pricing_model', 'per_unit')

        for field in base_required_fields:
            if field not in rule:
                errors.append(f"{rule_id}: Missing required field '{field}'")

        # unit_cost is required only for per_unit pricing model
        if pricing_model == 'per_unit' and 'unit_cost' not in rule:
            errors.append(f"{rule_id}: Missing required field 'unit_cost' for per_unit pricing")

        # Validate pricing_model value
        if pricing_model not in ['per_unit', 'hourly']:
            errors.append(f"{rule_id}: Invalid pricing_model '{pricing_model}' (must be 'per_unit' or 'hourly')")

        if 'lookback_seconds' in rule and rule['lookback_seconds'] < 60:
            warnings.append(f"{rule_id}: lookback_seconds < 60 may have insufficient data")
        
        if 'unit_cost' in rule:
            try:
                Decimal(str(rule['unit_cost']))
            except:
                errors.append(f"{rule_id}: Invalid unit_cost format")
        
        if 'threshold' in rule:
            try:
                Decimal(str(rule['threshold']))
            except:
                errors.append(f"{rule_id}: Invalid threshold format")
        
        valid_actions = ['throttle_lambda', 'disable_lambda', 'scale_down',
                        'notify_sns', 'start_step_function', 'set_reserved_concurrency',
                        'stop_ec2', 'stop_rds']
        if 'remediation_action' in rule and rule['remediation_action'] not in valid_actions:
            errors.append(f"{rule_id}: Unknown remediation_action '{rule['remediation_action']}'")
    
    if warnings:
        print("Warnings:")
        for w in warnings:
            print(f"  - {w}")
        print()
    
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  - {e}")
        return 1
    
    print(f"Configuration valid: {len(config['rules'])} rules")
    return 0


def cmd_generate_rule(args):
    """Generate a rule template for a given service."""
    templates = {
        'lambda': {
            'rule_id': 'lambda-cost-guard',
            'metric_namespace': 'AWS/Lambda',
            'metric_name': 'Invocations',
            'dimensions': [{'Name': 'FunctionName', 'Value': 'YOUR_FUNCTION_NAME'}],
            'lookback_seconds': 120,
            'projection_seconds': 600,
            'unit_cost': '0.0000002',
            'threshold': '5.00',
            'statistic': 'Sum',
            'period': 60,
            'remediation_action': 'throttle_lambda',
            'remediation_params': {
                'function_name': 'YOUR_FUNCTION_NAME',
                'concurrency': 10
            }
        },
        'apigateway': {
            'rule_id': 'apigateway-cost-guard',
            'metric_namespace': 'AWS/ApiGateway',
            'metric_name': 'Count',
            'dimensions': [{'Name': 'ApiName', 'Value': 'YOUR_API_NAME'}],
            'lookback_seconds': 300,
            'projection_seconds': 3600,
            'unit_cost': '0.0000035',
            'threshold': '10.00',
            'statistic': 'Sum',
            'period': 60,
            'remediation_action': 'notify_sns',
            'remediation_params': {
                'topic_arn': 'arn:aws:sns:REGION:ACCOUNT:TOPIC'
            }
        },
        'dynamodb': {
            'rule_id': 'dynamodb-cost-guard',
            'metric_namespace': 'AWS/DynamoDB',
            'metric_name': 'ConsumedWriteCapacityUnits',
            'dimensions': [{'Name': 'TableName', 'Value': 'YOUR_TABLE_NAME'}],
            'lookback_seconds': 300,
            'projection_seconds': 1800,
            'unit_cost': '0.00000125',
            'threshold': '25.00',
            'statistic': 'Sum',
            'period': 60,
            'remediation_action': 'notify_sns',
            'remediation_params': {
                'topic_arn': 'arn:aws:sns:REGION:ACCOUNT:TOPIC'
            }
        },
        'sqs': {
            'rule_id': 'sqs-cost-guard',
            'metric_namespace': 'AWS/SQS',
            'metric_name': 'NumberOfMessagesSent',
            'dimensions': [{'Name': 'QueueName', 'Value': 'YOUR_QUEUE_NAME'}],
            'lookback_seconds': 120,
            'projection_seconds': 600,
            'unit_cost': '0.0000004',
            'threshold': '2.00',
            'statistic': 'Sum',
            'period': 60,
            'remediation_action': 'notify_sns',
            'remediation_params': {
                'topic_arn': 'arn:aws:sns:REGION:ACCOUNT:TOPIC'
            }
        },
        'ec2': {
            'rule_id': 'ec2-cost-guard',
            'description': 'Monitor EC2 instance costs and stop instances when threshold breached',
            'metric_namespace': 'AWS/EC2',
            'metric_name': 'CPUUtilization',
            'dimensions': [],
            'lookback_seconds': 300,
            'projection_seconds': 3600,
            'pricing_model': 'hourly',
            'threshold': '25.00',
            'fallback_hourly_cost': '0.10',
            'statistic': 'Average',
            'period': 300,
            'instance_filter': {
                'ec2_filters': [
                    {'Name': 'tag:Environment', 'Values': ['development', 'staging']}
                ]
            },
            'remediation_action': 'stop_ec2',
            'remediation_params': {
                'notify_before': True,
                'topic_arn': 'arn:aws:sns:REGION:ACCOUNT:TOPIC'
            }
        },
        'ec2-runaway': {
            'rule_id': 'ec2-runaway-detection',
            'description': 'Detect unexpected EC2 instance launches and notify',
            'metric_namespace': 'AWS/EC2',
            'metric_name': 'CPUUtilization',
            'dimensions': [],
            'lookback_seconds': 300,
            'projection_seconds': 3600,
            'pricing_model': 'hourly',
            'threshold': '100.00',
            'fallback_hourly_cost': '0.10',
            'statistic': 'Average',
            'period': 60,
            'instance_filter': {
                'ec2_filters': []
            },
            'remediation_action': 'notify_sns',
            'remediation_params': {
                'topic_arn': 'arn:aws:sns:REGION:ACCOUNT:TOPIC'
            }
        },
        'rds': {
            'rule_id': 'rds-cost-guard',
            'description': 'Monitor RDS instance costs and stop instances when threshold breached',
            'metric_namespace': 'AWS/RDS',
            'metric_name': 'CPUUtilization',
            'dimensions': [],
            'lookback_seconds': 300,
            'projection_seconds': 3600,
            'pricing_model': 'hourly',
            'threshold': '15.00',
            'fallback_hourly_cost': '0.15',
            'statistic': 'Average',
            'period': 300,
            'instance_filter': {
                'rds_filters': {}
            },
            'remediation_action': 'stop_rds',
            'remediation_params': {
                'notify_before': True,
                'topic_arn': 'arn:aws:sns:REGION:ACCOUNT:TOPIC'
            }
        }
    }
    
    if args.service not in templates:
        print(f"Unknown service: {args.service}")
        print(f"Available: {', '.join(templates.keys())}")
        return 1
    
    template = templates[args.service]
    print(json.dumps(template, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(
        description='Cost Guardian CLI - Test and validate cost projection rules'
    )
    parser.add_argument('--region', default='us-east-1', help='AWS region')
    
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # evaluate command
    eval_parser = subparsers.add_parser('evaluate', help='Evaluate rules against live metrics')
    eval_parser.add_argument('--config', required=True, help='Path to rules.json')
    eval_parser.add_argument('--dry-run', action='store_true', help='Skip remediation execution')
    
    # simulate command
    sim_parser = subparsers.add_parser('simulate', help='Simulate cost projection')
    sim_parser.add_argument('--lookback', type=int, default=120, help='Lookback seconds')
    sim_parser.add_argument('--projection', type=int, default=600, help='Projection seconds')
    sim_parser.add_argument('--rate', type=float, required=True, help='Events per second')
    sim_parser.add_argument('--unit-cost', required=True, help='Cost per unit')
    sim_parser.add_argument('--threshold', required=True, help='Cost threshold')
    
    # pricing command
    price_parser = subparsers.add_parser('pricing', help='Get AWS pricing')
    price_parser.add_argument('--service', required=True, help='Service name (lambda)')
    
    # validate command
    val_parser = subparsers.add_parser('validate', help='Validate rules configuration')
    val_parser.add_argument('--config', required=True, help='Path to rules.json')
    
    # generate command
    gen_parser = subparsers.add_parser('generate', help='Generate rule template')
    gen_parser.add_argument('--service', required=True, help='Service name')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return 1
    
    commands = {
        'evaluate': cmd_evaluate,
        'simulate': cmd_simulate,
        'pricing': cmd_get_pricing,
        'validate': cmd_validate,
        'generate': cmd_generate_rule
    }
    
    return commands[args.command](args)


if __name__ == '__main__':
    sys.exit(main())
