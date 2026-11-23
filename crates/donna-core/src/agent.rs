use crate::events::{AgentEvent, EventSink};
use crate::types::{Context, Input, Route, RouteLabel, Trace};

pub trait Agent {
    fn run(&self, input: &Input, ctx: &Context, events: &mut dyn EventSink) -> Trace;
}

/// Always routes `general` at confidence `0.0` with no tools.
#[derive(Debug, Default, Clone, Copy)]
pub struct StubAgent;

impl Agent for StubAgent {
    fn run(&self, _input: &Input, _ctx: &Context, events: &mut dyn EventSink) -> Trace {
        let route = Route {
            label: RouteLabel::General,
            confidence: 0.0,
        };
        events.emit(AgentEvent::Routed(route.clone()));
        events.emit(AgentEvent::Done);
        Trace::stub(route)
    }
}
