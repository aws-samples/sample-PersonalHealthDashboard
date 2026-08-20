import boto3
import json
import os
from datetime import datetime, timedelta

# AWS Health Organizational View is only available via the
# us-east-1 endpoint, but the region comes from an env var driven
# by the stack's own region parameter, not hardcoded here.
health_client = boto3.client('health', region_name=os.environ['HEALTH_API_REGION'])
s3_client = boto3.client('s3')

DASHBOARD_BUCKET = os.environ['DASHBOARD_BUCKET']


def lambda_handler(event, context):
    # event/context are required by the Lambda runtime signature even
    # though this function doesn't branch on their contents - it always
    # runs the same fetch/parse/upload flow regardless of trigger source
    # (EventBridge schedule). event is logged below for CloudWatch visibility.
    print(f"Received event: {json.dumps(event)[:500]}")

    events = fetch_health_events()
    parsed = parse_events(events)
    build_and_upload_dashboard(parsed)

    result = {
        'events_processed': len(events),
        'timestamp': datetime.utcnow().isoformat()
    }
    return {'statusCode': 200, 'body': json.dumps(result)}


# ========== FETCH (STEP 1: download events, org-wide only) ==========

def fetch_health_events():
    """
    Fetches events across the whole AWS Organization via the Health
    Organizational View APIs. No single-account fallback - if org
    access isn't available, this raises and the run fails loudly
    rather than silently reporting a partial (single-account) view.
    """
    return fetch_org_events()


def get_affected_entities(event_arn, affected_accounts):
    entities = []
    for acct_id in (affected_accounts or []):
        try:
            paginator = health_client.get_paginator('describe_affected_entities_for_organization')
            for page in paginator.paginate(
                organizationEntityFilters=[{'eventArn': event_arn, 'awsAccountId': acct_id}]
            ):
                for entity in page.get('entities', []):
                    last_updated = entity.get('lastUpdatedTime')
                    entities.append({
                        'entityValue': entity.get('entityValue', 'N/A'),
                        'awsAccountId': entity.get('awsAccountId', acct_id),
                        'statusCode': entity.get('statusCode', ''),
                        'lastUpdatedTime': last_updated.isoformat() if last_updated else '',
                    })
        except Exception as e:
            print(f"Org entity fetch failed for {acct_id}/{event_arn}: {str(e)}")

    return entities


def get_event_description(event_arn, affected_accounts):
    for acct_id in (affected_accounts or []):
        try:
            response = health_client.describe_event_details_for_organization(
                organizationEventDetailFilters=[{'eventArn': event_arn, 'awsAccountId': acct_id}]
            )
            for detail in response.get('successfulSet', []):
                desc = detail.get('eventDescription', {}).get('latestDescription', '')
                if desc:
                    return desc
        except Exception as e:
            print(f"Org description fetch failed for {acct_id}/{event_arn}: {str(e)}")
    return ''


def fetch_org_events():
    events = []
    start_time = datetime.utcnow() - timedelta(days=7)

    paginator = health_client.get_paginator('describe_events_for_organization')
    filter_params = {
        'filter': {
            'startTime': {'from': start_time},
            # Closed events are intentionally excluded - only
            # currently relevant events are fetched and rendered.
            'eventStatusCodes': ['open', 'upcoming']
        }
    }

    for page in paginator.paginate(**filter_params):
        for evt in page.get('events', []):
            event_arn = evt['arn']
            start_time_val = evt.get('startTime', datetime.utcnow())
            end_time_val = evt.get('endTime')

            item = {
                'eventArn': event_arn,
                'service': evt.get('service', 'N/A'),
                'eventTypeCode': evt.get('eventTypeCode', ''),
                'eventTypeCategory': evt.get('eventTypeCategory', ''),
                'region': evt.get('region', 'global'),
                'startTime': start_time_val.isoformat() if hasattr(start_time_val, 'isoformat') else str(start_time_val),
                'endTime': end_time_val.isoformat() if end_time_val and hasattr(end_time_val, 'isoformat') else '',
                'statusCode': evt.get('statusCode', 'unknown'),
            }

            try:
                affected_resp = health_client.describe_affected_accounts_for_organization(eventArn=event_arn)
                item['affectedAccounts'] = affected_resp.get('affectedAccounts', [])
            except Exception as e:
                print(f"Affected accounts fetch failed for {event_arn}: {str(e)}")
                item['affectedAccounts'] = []

            item['affectedEntities'] = get_affected_entities(event_arn, item['affectedAccounts'])
            item['description'] = get_event_description(event_arn, item['affectedAccounts'])

            events.append(item)

    print(f"Org view: fetched {len(events)} events (open/upcoming only)")
    return events


# ========== PARSE (STEP 2) ==========

def parse_events(events):
    # Only 'open' and 'upcoming' ever arrive here (closed events
    # are excluded at the Health API query itself).
    order = {'open': 0, 'upcoming': 1}
    sorted_events = sorted(
        events,
        key=lambda e: (order.get(e.get('statusCode'), 2), e.get('startTime', ''))
    )

    total_resources = sum(
        len(e.get('affectedEntities', [])) for e in sorted_events
        if e.get('statusCode') in ('open', 'upcoming')
    )

    affected_accounts = sorted(set(
        acct for e in sorted_events for acct in e.get('affectedAccounts', [])
        if e.get('statusCode') in ('open', 'upcoming')
    ))

    summary = {
        'total_events': len(sorted_events),
        'open_issues': len([e for e in sorted_events if e.get('statusCode') == 'open' and e.get('eventTypeCategory') == 'issue']),
        'upcoming_changes': len([e for e in sorted_events if e.get('statusCode') == 'upcoming']),
        'affected_accounts': affected_accounts,
        'total_affected_resources': total_resources
    }

    return {'events': sorted_events, 'summary': summary}


# ========== RENDER + UPLOAD (STEP 3) ==========

def html_escape(s):
    if s is None:
        return ''
    return (str(s).replace('&', '&amp;').replace('<', '&lt;')
            .replace('>', '&gt;').replace('"', '&quot;'))


def fmt_cat(c):
    return {'issue': 'Issue', 'scheduledChange': 'Scheduled', 'accountNotification': 'Notification'}.get(c, c or '-')


def render_event_rows(events):
    rows = []
    for i, ev in enumerate(events):
        ents = ev.get('affectedEntities', []) or []
        accts = ev.get('affectedAccounts', []) or []
        evt_name = html_escape((ev.get('eventTypeCode') or '').replace('_', ' '))
        status = html_escape(ev.get('statusCode'))
        category = html_escape(ev.get('eventTypeCategory'))
        service = html_escape(ev.get('service') or '-')
        region = html_escape(ev.get('region') or 'global')
        start_time = html_escape(ev.get('startTime') or '-')
        res_count = f'<span class="resource-count">{len(ents)}</span>' if ents else '-'

        rows.append(f'''<tr id="r{i}">
            <td><button class="expand-btn" onclick="toggle({i})">&#9654;</button></td>
            <td><strong>{service}</strong></td>
            <td style="max-width:260px">{evt_name}</td>
            <td><span class="badge {category}">{fmt_cat(ev.get('eventTypeCategory'))}</span></td>
            <td>{region}</td>
            <td><span class="badge {status}">{status}</span></td>
            <td>{res_count}</td>
            <td>{len(accts)}</td>
            <td style="white-space:nowrap">{start_time}</td>
        </tr>''')

        desc_html = (f'<div class="desc-box">{html_escape(ev.get("description"))}</div>'
                     if ev.get('description') else '<p class="no-data">No description available for this event</p>')
        accts_html = (f'<p class="acct-list">{html_escape(", ".join(accts))}</p>'
                      if accts else '<p class="no-data">None</p>')

        if ents:
            res_rows = ''.join(
                f'''<tr>
                    <td><code>{html_escape(en.get('entityValue', 'N/A'))}</code></td>
                    <td>{html_escape(en.get('awsAccountId') or '-')}</td>
                    <td><span class="res-badge {html_escape(en.get('statusCode') or '')}">{html_escape(en.get('statusCode') or '-')}</span></td>
                    <td>{html_escape(en.get('lastUpdatedTime') or '-')}</td>
                </tr>'''
                for en in ents
            )
            res_html = f'''<table class="res-table"><thead><tr>
                <th>Resource ID</th><th>Account</th><th>Status</th><th>Last Updated</th>
            </tr></thead><tbody>{res_rows}</tbody></table>'''
        else:
            res_html = '<p class="no-data">No specific resources identified &mdash; this is an account-level notification</p>'

        rows.append(f'''<tr class="detail-row" id="d{i}"><td colspan="9" class="detail-cell"><div class="detail-inner">
            <div class="detail-section"><h4>&#128196; Description</h4>{desc_html}</div>
            <div class="detail-section"><h4>&#127970; Affected Accounts ({len(accts)})</h4>{accts_html}</div>
            <div class="detail-section"><h4>&#128421; Affected Resources ({len(ents)})</h4>{res_html}</div>
        </div></td></tr>''')

    return ''.join(rows) if events else '<tr><td colspan="9" class="loading">No events found</td></tr>'


def build_and_upload_dashboard(parsed):
    events = parsed['events']
    summary = parsed['summary']
    rows_html = render_event_rows(events)
    generated_at = datetime.utcnow().strftime('%d %b %Y %H:%M UTC')

    html_content = DASHBOARD_HTML_TEMPLATE \
        .replace('{{ROWS}}', rows_html) \
        .replace('{{K_ISSUES}}', str(summary['open_issues'])) \
        .replace('{{K_UPCOMING}}', str(summary['upcoming_changes'])) \
        .replace('{{K_TOTAL}}', str(summary['total_events'])) \
        .replace('{{K_ACCOUNTS}}', str(len(summary['affected_accounts']))) \
        .replace('{{K_RESOURCES}}', str(summary['total_affected_resources'])) \
        .replace('{{GENERATED_AT}}', generated_at)

    s3_client.put_object(
        Bucket=DASHBOARD_BUCKET,
        Key='index.html',
        Body=html_content.encode('utf-8'),
        ContentType='text/html',
        CacheControl='no-cache, max-age=60'
    )
    print(f"Uploaded refreshed index.html ({len(events)} events) to {DASHBOARD_BUCKET}")


DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AWS Health Dashboard</title>
    <style>
        :root {
            --bg: #0d1117; --bg-card: #161b22; --bg-hover: #1c2430; --border: #30363d;
            --text: #e6edf3; --text-sec: #8b949e; --text-muted: #656d76;
            --primary: #58a6ff; --success: #3fb950; --error: #f85149; --warning: #d29922;
            --info: #79c0ff; --orange: #ff9900;
            --font: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            --mono: 'SF Mono', 'Fira Code', Consolas, monospace; --radius: 8px;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: var(--bg); color: var(--text); font-family: var(--font); min-height: 100vh; }
        .header { background: linear-gradient(135deg, #161b22, #1c2430); border-bottom: 1px solid var(--border); padding: 16px 28px; display: flex; justify-content: space-between; align-items: center; }
        .header-left { display: flex; align-items: center; gap: 14px; }
        .header-logo { width: 38px; height: 38px; background: linear-gradient(135deg, #ff9900, #ffad33); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 18px; font-weight: 700; color: #000; }
        .header h1 { font-size: 18px; font-weight: 600; }
        .header .sub { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
        .header-right { display: flex; align-items: center; gap: 16px; font-size: 11px; color: var(--text-muted); }
        .container { max-width: 1440px; margin: 0 auto; padding: 24px; }
        .kpi-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; margin-bottom: 24px; }
        @media (max-width: 900px) { .kpi-row { grid-template-columns: repeat(2, 1fr); } }
        .kpi { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); padding: 18px; position: relative; overflow: hidden; }
        .kpi::after { content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px; }
        .kpi.red::after { background: var(--error); } .kpi.yellow::after { background: var(--warning); }
        .kpi.blue::after { background: var(--primary); } .kpi.green::after { background: var(--success); }
        .kpi.orange::after { background: var(--orange); }
        .kpi-val { font-size: 30px; font-weight: 700; font-family: var(--mono); margin-bottom: 4px; }
        .kpi.red .kpi-val { color: var(--error); } .kpi.yellow .kpi-val { color: var(--warning); }
        .kpi.blue .kpi-val { color: var(--primary); } .kpi.green .kpi-val { color: var(--success); }
        .kpi.orange .kpi-val { color: var(--orange); }
        .kpi-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
        .table-wrap { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
        .table-title { padding: 14px 20px; font-size: 14px; font-weight: 600; border-bottom: 1px solid var(--border); }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; padding: 10px 16px; font-size: 11px; color: var(--text-muted); text-transform: uppercase; background: #0d1117; border-bottom: 1px solid var(--border); }
        td { padding: 12px 16px; font-size: 13px; border-bottom: 1px solid var(--border); vertical-align: top; }
        tr:hover td { background: var(--bg-hover); }
        .badge { display: inline-block; padding: 3px 8px; border-radius: 4px; font-size: 11px; font-weight: 500; }
        .badge.open { background: rgba(248,81,73,0.12); color: var(--error); }
        .badge.upcoming { background: rgba(210,153,34,0.12); color: var(--warning); }
        .badge.closed { background: rgba(63,185,80,0.12); color: var(--success); }
        .badge.issue { background: rgba(248,81,73,0.1); color: var(--error); }
        .badge.scheduledChange { background: rgba(121,192,255,0.1); color: var(--info); }
        .badge.accountNotification { background: rgba(210,153,34,0.1); color: var(--warning); }
        .resource-count { font-family: var(--mono); font-weight: 600; color: var(--orange); }
        .expand-btn { background: none; border: none; color: var(--primary); cursor: pointer; font-size: 13px; padding: 4px 6px; border-radius: 4px; }
        .expand-btn:hover { background: rgba(88,166,255,0.1); }
        .detail-row { display: none; } .detail-row.open { display: table-row; }
        .detail-cell { padding: 0 16px 16px 16px; background: #0d1117; }
        .detail-inner { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px; margin-top: 8px; }
        .detail-section { margin-bottom: 14px; } .detail-section:last-child { margin-bottom: 0; }
        .detail-section h4 { font-size: 12px; font-weight: 600; color: var(--orange); margin-bottom: 8px; display: flex; align-items: center; gap: 6px; }
        .desc-box { background: var(--bg); padding: 12px; border-radius: 6px; font-size: 12px; line-height: 1.7; color: var(--text-sec); max-height: 200px; overflow-y: auto; white-space: pre-wrap; border: 1px solid var(--border); }
        .acct-list { font-size: 12px; font-family: var(--mono); color: var(--text-sec); }
        .res-table { width: 100%; border-collapse: collapse; font-size: 12px; }
        .res-table th { background: #161b22; padding: 8px 10px; font-size: 10px; color: var(--text-muted); border-bottom: 1px solid var(--border); }
        .res-table td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
        .res-table code { background: var(--bg); padding: 2px 6px; border-radius: 3px; font-size: 11px; color: var(--info); }
        .res-badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-size: 10px; font-weight: 600; }
        .res-badge.IMPAIRED { background: rgba(248,81,73,0.12); color: var(--error); }
        .res-badge.UNIMPAIRED { background: rgba(63,185,80,0.12); color: var(--success); }
        .res-badge.PENDING { background: rgba(210,153,34,0.12); color: var(--warning); }
        .no-data { color: var(--text-muted); font-style: italic; font-size: 12px; }
        .loading { text-align: center; padding: 50px; color: var(--text-muted); }
    </style>
</head>
<body>
<div class="header">
    <div class="header-left">
        <div class="header-logo">H</div>
        <div><h1>AWS Health Dashboard</h1><div class="sub">Organization Health View</div></div>
    </div>
    <div class="header-right">Generated {{GENERATED_AT}}</div>
</div>
<div class="container">
    <div class="kpi-row">
        <div class="kpi red"><div class="kpi-val">{{K_ISSUES}}</div><div class="kpi-label">Open Issues</div></div>
        <div class="kpi yellow"><div class="kpi-val">{{K_UPCOMING}}</div><div class="kpi-label">Upcoming Changes</div></div>
        <div class="kpi blue"><div class="kpi-val">{{K_TOTAL}}</div><div class="kpi-label">Total Events</div></div>
        <div class="kpi green"><div class="kpi-val">{{K_ACCOUNTS}}</div><div class="kpi-label">Accounts Affected</div></div>
        <div class="kpi orange"><div class="kpi-val">{{K_RESOURCES}}</div><div class="kpi-label">Resources Affected</div></div>
    </div>
    <div class="table-wrap">
        <div class="table-title"><span style="margin-right:8px;color:var(--orange)">&#9776;</span>Health Events</div>
        <table>
            <thead><tr>
                <th style="width:36px"></th><th>Service</th><th>Event</th><th>Category</th>
                <th>Region</th><th>Status</th><th>Resources</th><th>Accounts</th><th>Start Time</th>
            </tr></thead>
            <tbody>{{ROWS}}</tbody>
        </table>
    </div>
</div>
<script>
function toggle(i) {
    const row = document.getElementById('d'+i);
    const btn = document.querySelector('#r'+i+' .expand-btn');
    if (row.classList.contains('open')) { row.classList.remove('open'); btn.innerHTML = '&#9654;'; }
    else { row.classList.add('open'); btn.innerHTML = '&#9660;'; }
}
</script>
</body>
</html>"""
