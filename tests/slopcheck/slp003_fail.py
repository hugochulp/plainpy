# expect: Circle, Point, Shape
from pydantic import BaseModel


class Point(BaseModel):  # built and read in code only: a dataclass would do
    x: float
    y: float


class Shape(BaseModel):
    name: str


class Circle(Shape):
    centre: Point
    radius: float


def area(circle: Circle) -> float:
    return 3.14159 * circle.radius**2


def unit_circle() -> Circle:
    circle = Circle(name="unit", centre=Point(x=0, y=0), radius=1)
    assert isinstance(circle, Circle)  # a class passed to isinstance doesn't reach I/O
    return circle
