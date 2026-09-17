output "instance_public_ip" {
  description = "Elastic IP address of the application instance."
  value       = aws_eip.app.public_ip
}

output "backup_bucket_name" {
  description = "Name of the private S3 bucket for database backups."
  value       = aws_s3_bucket.backups.bucket
}

output "ssh_command" {
  description = "Command to connect to the application instance."
  value       = "ssh -i ${trimsuffix(var.ssh_public_key_path, ".pub")} ubuntu@${aws_eip.app.public_ip}"
}
