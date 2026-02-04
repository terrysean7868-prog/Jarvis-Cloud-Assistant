"""
Mode-Aware Decision Maker
Integrates the mode engine with the existing decision maker
to provide adaptive decision-making across different operational modes
"""

import asyncio
from typing import Dict, Any, Optional
from datetime import datetime

from src.core.mode_decision_engine import ModeDecisionEngine, OperationalMode, get_mode_engine
from src.core.decision_maker import ContextAwareDecisionMaker, get_decision_maker


class ModeAwareDecisionMaker:
    """
    Combines mode-aware decisions with context-aware decisions
    Provides unified decision-making system
    """
    
    def __init__(self):
        self.mode_engine = None
        self.context_maker = None
        self.decision_cache = {}
        self.mode_switch_callbacks = []
        
    async def initialize(self):
        """Initialize both engines"""
        self.mode_engine = await get_mode_engine()
        self.context_maker = await get_decision_maker()
        
    async def switch_mode(self, mode: str) -> Dict[str, Any]:
        """
        Switch operational mode
        
        Args:
            mode: One of 'learn', 'update', 'execute', 'analyze', 'develop', 'creative', 'interact'
            
        Returns:
            Mode change result with description and priorities
        """
        try:
            op_mode = OperationalMode(mode.lower())
        except ValueError:
            return {
                "status": "error",
                "message": f"Invalid mode '{mode}'. Available: {', '.join([m.value for m in OperationalMode])}",
                "available_modes": [m.value for m in OperationalMode]
            }
        
        result = self.mode_engine.set_mode(op_mode)
        
        # Trigger callbacks
        for callback in self.mode_switch_callbacks:
            await callback(op_mode)
        
        return result
    
    async def make_decision(
        self,
        instruction: str,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Make a decision considering both mode and context
        
        Args:
            instruction: User instruction text
            context: Optional context (pc_info, web_results, etc.)
            
        Returns:
            Comprehensive decision with mode and context awareness
        """
        if not self.context_maker:
            await self.initialize()
        
        # Parse instruction with context maker
        parsed = await self.context_maker.parse_instruction(instruction)
        
        # Get mode-based decision
        mode_decision = self.mode_engine.make_decision(instruction, parsed)
        
        # Get context-based decision
        context_decision = await self.context_maker.decide_action(instruction)
        
        # Merge decisions (mode takes priority)
        merged_decision = self._merge_decisions(
            mode_decision,
            context_decision,
            parsed
        )
        
        return merged_decision
    
    def _merge_decisions(
        self,
        mode_decision: Dict[str, Any],
        context_decision: Dict[str, Any],
        parsed_intent: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Intelligently merge mode and context decisions"""
        
        # If mode decision has high confidence, use it
        mode_confidence = mode_decision.get("confidence", 0.0)
        context_confidence = context_decision.get("confidence", 0.0)
        
        # Mode takes 60% weight, context takes 40% weight
        final_confidence = (mode_confidence * 0.6) + (context_confidence * 0.4)
        
        # Determine primary recommendation
        if mode_confidence > 0.85:
            primary = mode_decision
            source = "mode"
        elif context_confidence > 0.85:
            primary = context_decision
            source = "context"
        else:
            # Use whichever has higher confidence
            if mode_confidence > context_confidence:
                primary = mode_decision
                source = "mode"
            else:
                primary = context_decision
                source = "context"
        
        return {
            "recommended_action": primary.get("recommended_action"),
            "recommended_action_type": primary.get("recommended_action_type"),
            "confidence": final_confidence,
            "source": source,
            "mode": self.mode_engine.current_mode.value if self.mode_engine else "unknown",
            "reasoning": primary.get("reasoning", ""),
            "mode_context": mode_decision.get("mode_context"),
            "system_context": context_decision.get("context"),
            "alternatives": primary.get("alternatives", []),
            "merged_reasoning": {
                "mode_decision": mode_decision.get("reasoning"),
                "context_decision": context_decision.get("reasoning"),
                "merge_strategy": f"Mode confidence {mode_confidence:.2f} vs Context {context_confidence:.2f}. "
                                 f"Selected {source} as primary source."
            },
            "parsed_intent": parsed_intent,
            "timestamp": datetime.now().isoformat()
        }
    
    def get_current_mode(self) -> str:
        """Get current operational mode"""
        if not self.mode_engine:
            return "interact"
        return self.mode_engine.current_mode.value
    
    def register_mode_switch_callback(self, callback):
        """Register callback for mode switches"""
        self.mode_switch_callbacks.append(callback)
    
    def get_mode_info(self) -> Dict[str, Any]:
        """Get current mode information"""
        if not self.mode_engine:
            return {}
        return self.mode_engine.get_mode_summary()
    
    async def explain_decision(self, instruction: str) -> Dict[str, Any]:
        """
        Explain how a decision was made - for debugging/learning
        
        Returns detailed information about all factors in the decision
        """
        if not self.context_maker:
            await self.initialize()
        
        parsed = await self.context_maker.parse_instruction(instruction)
        mode_decision = self.mode_engine.make_decision(instruction, parsed)
        context_decision = await self.context_maker.decide_action(instruction)
        
        return {
            "instruction": instruction,
            "mode_analysis": {
                "current_mode": self.mode_engine.current_mode.value,
                "mode_decision": mode_decision,
                "mode_priorities": self.mode_engine._get_mode_priorities_readable(
                    self.mode_engine.current_mode
                )
            },
            "context_analysis": {
                "parsed_intent": parsed,
                "context_decision": context_decision
            },
            "final_decision": await self.make_decision(instruction),
            "timestamp": datetime.now().isoformat()
        }


# Global singleton
_mode_aware_maker = None

async def initialize_mode_aware_decision_maker() -> ModeAwareDecisionMaker:
    """Initialize the global mode-aware decision maker"""
    global _mode_aware_maker
    _mode_aware_maker = ModeAwareDecisionMaker()
    await _mode_aware_maker.initialize()
    return _mode_aware_maker

async def get_mode_aware_decision_maker() -> ModeAwareDecisionMaker:
    """Get the mode-aware decision maker instance"""
    global _mode_aware_maker
    if _mode_aware_maker is None:
        _mode_aware_maker = ModeAwareDecisionMaker()
        await _mode_aware_maker.initialize()
    return _mode_aware_maker
