from abc import ABC, abstractmethod

class JobRepository(ABC):
    @abstractmethod
    def save(self, job): pass
    @abstractmethod
    def get_all(self): pass

class LLMClient(ABC):
    @abstractmethod
    def evaluate(self, prompt): pass

class JobSearch(ABC):
    @abstractmethod
    def search(self, query): pass