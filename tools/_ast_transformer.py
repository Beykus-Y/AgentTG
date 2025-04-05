# tools/_ast_transformer.py
import ast
import logging
from typing import Any

logger = logging.getLogger(__name__)

class ReplaceCodeTransformer(ast.NodeTransformer):
    """AST Transformer для замены узла (функции или класса)."""
    def __init__(self, block_type: str, block_name: str, new_code_node: ast.AST):
        self.block_type = block_type
        self.block_name = block_name
        self.new_code_node = new_code_node
        self.replaced = False # Флаг, была ли произведена замена

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        """Посещает узел определения функции."""
        if self.block_type == "function" and node.name == self.block_name:
            if isinstance(self.new_code_node, ast.FunctionDef):
                logger.info(f"Replacing function '{self.block_name}'.")
                # Сохраняем декораторы старой функции, если у новой их нет
                if node.decorator_list and not self.new_code_node.decorator_list:
                    self.new_code_node.decorator_list = node.decorator_list
                self.replaced = True
                return self.new_code_node # Возвращаем новый узел
            else:
                logger.error(f"AST Type Mismatch: Expected FunctionDef for '{self.block_name}', got {type(self.new_code_node)}.")
                return node # Возвращаем старый узел при ошибке типа
        return self.generic_visit(node) # Продолжаем обход для вложенных узлов

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        """Посещает узел определения класса."""
        if self.block_type == "class" and node.name == self.block_name:
            if isinstance(self.new_code_node, ast.ClassDef):
                logger.info(f"Replacing class '{self.block_name}'.")
                # Сохраняем декораторы старого класса, если у нового их нет
                if node.decorator_list and not self.new_code_node.decorator_list:
                    self.new_code_node.decorator_list = node.decorator_list
                self.replaced = True
                return self.new_code_node # Возвращаем новый узел
            else:
                logger.error(f"AST Type Mismatch: Expected ClassDef for '{self.block_name}', got {type(self.new_code_node)}.")
                return node # Возвращаем старый узел при ошибке типа
        return self.generic_visit(node) # Продолжаем обход для вложенных узлов 