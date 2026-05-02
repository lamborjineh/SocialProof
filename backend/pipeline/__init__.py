"""
SocialProof — Pipeline package
Exports AnalysisPipeline so routers can import it with:
    from pipeline import AnalysisPipeline
"""

from .orchestrator import AnalysisPipeline

__all__ = ["AnalysisPipeline"]
