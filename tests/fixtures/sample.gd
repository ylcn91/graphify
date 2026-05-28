extends CharacterBody2D

class_name Player

const MAX_HEALTH = 100
var health: int = MAX_HEALTH
var speed := 250.0
signal died

@export var jump_force: float = 400.0


func _ready():
    reset()


func reset():
    health = MAX_HEALTH


func take_damage(amount):
    health -= amount
    if health <= 0:
        die()


func die():
    emit_signal("died")
    queue_free()


class Inventory:
    var items = []

    func add(item):
        items.append(item)
