output "nameservers" {
  description = "Paste these at the registrar. This is the one step that cannot be scripted, and DNS does not move until it happens."
  value       = var.create_hosted_zone ? aws_route53_zone.main[0].name_servers : data.aws_route53_zone.existing[0].name_servers
}

output "public_ip" {
  description = "Elastic IP. Useful for testing over --resolve before the nameserver change lands."
  value       = aws_eip.app.public_ip
}

output "instance_id" {
  value = aws_instance.app.id
}

output "ssm_command" {
  description = "How to get a shell without an open SSH port."
  value       = "aws ssm start-session --target ${aws_instance.app.id} --region ${var.region}"
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.id
}

output "log_groups" {
  value = [for group in aws_cloudwatch_log_group.box : group.name]
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
