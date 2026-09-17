variable "ssh_public_key_path" {
  description = "Path to the public key used for the EC2 key pair."
  type        = string
  default     = "~/.ssh/irish-rail-tracker.pub"
}

variable "alarm_email" {
  description = "Email address that receives AWS billing alarm notifications."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type for the application host."
  type        = string
  default     = "t3.micro"
}
