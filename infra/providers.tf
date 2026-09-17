provider "aws" {
  region = "eu-west-1"

  default_tags {
    tags = {
      Project     = "irish-rail-tracker"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = "irish-rail-tracker"
      Environment = "production"
      ManagedBy   = "terraform"
    }
  }
}
