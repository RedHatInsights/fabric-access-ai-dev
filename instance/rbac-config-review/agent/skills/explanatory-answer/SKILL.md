---
name: explanatory-answer
description: Explain answers with direct reasoning, evidence, examples, and requested Mermaid diagrams when pinned context supports them.
---

# Explanatory Answer

Answer the question first. Then help the reader understand how the answer follows from the relevant facts. Use the order below for an explanation; a simple fact may need only the direct answer.

## Explain

1. **Answer:** Respond directly to the question.
2. **Why or how:** Explain the mechanism in a clear sequence.
3. **Reasoning:** Connect the relevant facts to the conclusion; cite evidence when needed. Identify material assumptions or uncertainty, and do not leave a key step for the reader to guess.
4. **Example:** Add one only if it makes the explanation easier to grasp.
5. **Implication:** Say what follows or what to do, when relevant.

Use these headings in this order when a longer answer benefits from structure. Omit optional sections when they add nothing, and do not repeat the same point under multiple headings.

## Optional diagram

Default to prose. If the user explicitly asks for a diagram or graph in an
explanation reply, include one when the pinned facts support an accurate
visualization, even if prose alone would be sufficient. Use a fenced Mermaid
block and choose a flowchart, sequence diagram, or other suitable form. Place
it after the explanation and any implication. If the user did not ask for a
diagram, add one only when it explains a relationship, branch, or sequence
substantially better than a few sentences.

Keep diagrams concise, use short labels, and ground every edge in the same
facts as the prose. GitHub renders Mermaid in pull request Markdown. If the
available context cannot support an accurate diagram, say what is missing
instead of inventing a relationship. If labels are clipped or the diagram
does not render clearly, simplify it or explain the relationship in text.
Diagrams supplement explanation replies; they do not belong in status
summaries or original finding comments, and their availability does not
require one when the user has not requested it.
