from pydantic import BaseModel, Field, SecretStr


class SourceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    dialect: str = Field(description="首版目标为 mysql")
    host: str
    port: int = Field(gt=0, le=65535)
    database: str
    username: str
    password: SecretStr
    schemas: list[str] = Field(default_factory=list)
    read_only: bool = True


class SourceConnectionTestRequest(BaseModel):
    dialect: str
    host: str
    port: int = Field(gt=0, le=65535)
    database: str
    username: str
    password: SecretStr
