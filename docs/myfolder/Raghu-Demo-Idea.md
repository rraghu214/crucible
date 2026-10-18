demo Scope, what my expectations are:
1. Start with a quick intro on what problem I'm trying to solve and show the onboarding steps , mention about a spring boot app I have deployed on Oracle and on another box, I have Crucible running. My GLC v5 gateway is hosted on render.
2. You prepare me something like a product doc (before the demo) which also has some sections that brief about the NFR too. 
3. I demo the upload, chunking. Remember, GLC v5 with embedders has not been tested. GLC code which is currently on render is available here locally - C:\Raghu\MyLearnings\EAG_V3\S16-08082026\assignment\glc_v5
render.com was sourced from 
https://github.com/rraghu214/glc_V5_RRAGHU214
4. when I chat for onboarding, my questions are more curated to my needs based on the details from the doc.
5. I should be able to view the yaml and / or all the yaml values from UI. All the yaml participating in the run. I hope this happens from 'New Test Run' tab and show the run for one of the modes that will make this product more savy - I feel fully automated mode and cite references that it is inspired from Andrej Karpathy's autoresearch (if you feel this is a good and accurate reference that I'm inspired by Andrej).
6. After which I show some past runs for advisory & semi-automated on how it has progressed and a word of mouth on ceiling probe.
7. Even before I begin the run, I should show how to upload the users.csv file and fact that this is not chunked / RAG and how I'm mapping the variables with the POST body template.
8. Based on the mode I have selected, the budget should also change accordingly. Also one small toggle that if I add more experiments, the budget also increases. I will show the options to cap the run. 
9. Not sure where on UI, I guess it is on the Active tab, it should print the real progress of the run - the logs and PDMA loop. I feel the crucible command can be a little more verbose with its logging and neatly formatted indicating each action it is taking for transparency.
10. After the run, I want to show the backend files, logs etc. I also want to talk about the 7 types of memory we have and how these concepts are leveraged in the code.
11. Show the guardrails and options to edit the properties or files that I want to edit before the run. Also, explain about the guardrails, using exclusive git branch to do the experiments, dedicated folder within the Box B as a local sandbox.
11. Benchmarks tab mostly looks good, but this is an important section, where I need to spend good time on the benchmarks for our harness. talking about benchmarks, fixtures, number of test cases in Crucible. This is the section that talks about how dense our application is.
12. I want to talk about evals too.
13. Before starting the run - I will explain and show the  preflight steps and also state that, these steps happen before start of any campaign too.
14. I want to also show the chat facility. I hope it is powerful enought to understand whats on the screen and comment about the current active run or if I'm asking about some info like
"What does this reveal Diff: TR-002 vs TR-003 that you see on the screen?"
I got the below response which feels off. The context matters, how do we attach the context? Be it onboarding step or questions on history & diff
🔥 "Based on what I can see right now, the connection pool is the bottleneck. Happy to dig into any specific signal."
15. I will also be demoing the progress on datadog UI and state that, metrics are being pulled by the agent via live polling too. It also integrates to the CI/CD pipeline to deploy the sandbox branch etc.
16. Is there a way to visualize the run's graphs from datadog or what locust provides on its UI via the CSV fies for both the active run and in the results tab?
17. And also a mention that every UI click is driven by a backend utility, wherever possible lets mention these commands in the UI, we should highlight that so if our app has to run in headless mode it is still possible.

Go through all these pdfs that covers on the session notes. I want to be able to associate and relate it to as many concepts from the course as possible, without explicitly mentioning that i'm doing this because the course thought me.

Access the subfolders here to get the full session transcript and notes
C:\Raghu\MyLearnings\EAG_V3\Consolidated

I should give a best and one shot final demo

Provide your outputs in the respective files within doc / myfolder for the demo