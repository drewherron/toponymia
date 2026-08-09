# Retention is set here rather than left to the agent, because the CloudWatch
# default is "never expire" and these logs hold client IPs. See
# log_retention_days in variables.tf for why the number is not arbitrary.
resource "aws_cloudwatch_log_group" "box" {
  for_each = toset(["gunicorn", "caddy", "postgres"])

  name              = "/toponymia/${each.key}"
  retention_in_days = var.log_retention_days
}

resource "aws_sns_topic" "alerts" {
  name = "toponymia-alerts"
}

# An alarm with no action is a dashboard nobody opens. If no address is given
# the alarms still exist and still fire, they just page nothing.
resource "aws_sns_topic_subscription" "alerts_email" {
  count = var.alert_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Django writes unhandled 500s to stderr unconditionally, which gunicorn
# captures — so this filter is what turns "the traceback exists somewhere" into
# "something told me". It is the top item on the first-week watch list.
resource "aws_cloudwatch_log_metric_filter" "django_errors" {
  name           = "django-request-errors"
  log_group_name = aws_cloudwatch_log_group.box["gunicorn"].name
  pattern        = "ERROR django.request"

  metric_transformation {
    name          = "DjangoRequestErrors"
    namespace     = "Toponymia"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "django_errors" {
  alarm_name          = "toponymia-500s"
  namespace           = "Toponymia"
  metric_name         = aws_cloudwatch_log_metric_filter.django_errors.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_description = "One or more unhandled 500s in five minutes."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}

# Postgres, the revision history and the logs all live on the root volume, so
# filling it takes down the database rather than just the logging.
resource "aws_cloudwatch_metric_alarm" "disk" {
  alarm_name          = "toponymia-disk-80"
  namespace           = "Toponymia"
  metric_name         = "disk_used_percent"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching" # No data here means the agent died, which is itself worth knowing.

  dimensions = {
    InstanceId = aws_instance.app.id
    path       = "/"
  }

  alarm_description = "Root volume above 80%."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "status_check" {
  alarm_name          = "toponymia-status-check"
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.app.id
  }

  alarm_description = "Instance or system status check failing."
  alarm_actions     = [aws_sns_topic.alerts.arn]
}
