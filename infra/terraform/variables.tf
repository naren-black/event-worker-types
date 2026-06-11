# Illustrative only - see README.md.

variable "bucket_name" {
  description = "Bucket name for processed order files (matches S3_BUCKET / GCS_BUCKET in .env.example)."
  type        = string
  default     = "wms-orders-processed"
}

variable "aws_region" {
  description = "AWS region for the S3 bucket and IAM resources."
  type        = string
  default     = "us-east-1"
}

variable "eks_oidc_provider_arn" {
  description = "ARN of the EKS cluster's IAM OIDC identity provider, used as the IRSA trust principal."
  type        = string
  default     = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLEID"
}

variable "eks_oidc_provider_url" {
  description = "Same OIDC provider as eks_oidc_provider_arn, without the ARN prefix - used in the trust policy condition key."
  type        = string
  default     = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLEID"
}

variable "k8s_namespace" {
  description = "Kubernetes namespace the worker runs in (used in the IRSA trust condition)."
  type        = string
  default     = "sftp-event-worker"
}

variable "k8s_service_account" {
  description = "Kubernetes service account bound to the IAM role via IRSA."
  type        = string
  default     = "sftp-event-worker"
}

variable "gcp_project_id" {
  description = "GCP project that owns the GCS bucket."
  type        = string
  default     = "demo-project"
}

variable "gcp_workload_identity_pool" {
  description = "Full resource name of the Workload Identity Federation pool the worker authenticates through."
  type        = string
  default     = "projects/123456789/locations/global/workloadIdentityPools/sftp-event-worker"
}
