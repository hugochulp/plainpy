# expect:
from fastapi import FastAPI
from openai import OpenAI
from pydantic import BaseModel
from pydantic_settings import BaseSettings


class Address(BaseModel):  # a field of User, which is validated
    city: str


class User(BaseModel):
    name: str
    address: Address


class Admin(User):  # a subclass of a model that reaches I/O
    level: int


class Answer(BaseModel):  # an LLM's structured output: the SDK takes the class
    text: str


class Item(BaseModel):  # in a route's signature
    sku: str


class Event(BaseModel):  # built from a dict
    kind: str


class Report(BaseModel):  # dumped
    total: int


class Search(BaseModel):  # its JSON schema is a tool definition
    query: str


class Config(BaseSettings):  # settings: pydantic reads them from the environment
    debug: bool = False


app = FastAPI()
TOOLS = [Search.model_json_schema()]


def load(text: str) -> User:
    return User.model_validate_json(text)


def ask(client: OpenAI, prompt: str) -> Answer | None:
    reply = client.chat.completions.parse(model="gpt", messages=[{"role": "user", "content": prompt}], response_format=Answer)
    return reply.choices[0].message.parsed


@app.post("/items")
def create(item: Item) -> str:
    return item.sku


def handle(data: dict[str, str]) -> Event:
    return Event(**data)


def save(report: Report) -> str:
    return report.model_dump_json()
