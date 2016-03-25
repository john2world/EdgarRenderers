# -*- coding: utf-8 -*-
"""
:mod:`EdgarRenderer.Filing`
~~~~~~~~~~~~~~~~~~~
Edgar(tm) Renderer was created by staff of the U.S. Securities and Exchange Commission.
Data and content created by government employees within the scope of their employment 
are not subject to domestic copyright protection. 17 U.S.C. 105.
"""

from gettext import gettext as _
from collections import defaultdict
import os, re, math, datetime, dateutil.relativedelta, lxml
import arelle.ModelValue, arelle.XbrlConst
import Cube, Embedding, Report, PresentationGroup, Summary, Utils, Xlout

def mainFun(controller, modelXbrl, outputFolderName):
    filing = Filing(controller, modelXbrl, outputFolderName)
    filing.populateAndLinkClasses()

    sortedCubeList = sorted(filing.cubeDict.values(), key=lambda cube : cube.definitionText)

    for cube in sortedCubeList:
        filing.cubeDriverBeforeFlowThroughSuppression(cube)
        if not cube.isEmbedded and not cube.noFactsOrAllFactsSuppressed:
            embedding = Embedding.Embedding(filing, cube, [])
            cube.embeddingList = [embedding]
            filing.embeddingDriverBeforeFlowThroughSuppression(embedding)

    if not filing.hasEmbeddings:
        filing.filterOutColumnsWhereAllElementsAreInOtherReports(sortedCubeList) # otherwise known as flow through suppression

    # this complicated way to number files is all about maintaining re2 compatibility
    nextFileNum = controller.nextFileNum
    for cube in sortedCubeList:
        if not cube.excludeFromNumbering and len(cube.factMemberships) > 0:
            # even though there is embedding, and cubes might have more than one embedding and thus more than one report,
            # we still keep the fileNumber attribute on the cube and not the report, because if there are multiple embeddings
            # they all print in one file.
            cube.fileNumber = nextFileNum
            nextFileNum += 1
            controller.logDebug(_('File R{!s} is {} {}').format(cube.fileNumber, cube.linkroleUri, cube.definitionText))

            # we keep track of embedded cubes so that we know later if for some cubes, no embeddings were actually embedded.
            if cube.isEmbedded:
                filing.embeddedCubeSet.add(cube)

    # handle excel writing
    xlWriter = None
    if controller.excelXslt:
        if filing.hasEmbeddings:
            controller.logDebug(_("Excel XSLT is not applied to instance {} having embedded commands.").format(
                                filing.fileNameBase))
        else:
            xlWriter = controller.xlWriter
            if not xlWriter:
                controller.xlWriter = xlWriter = Xlout.XlWriter(controller, outputFolderName)

    #import win32process
    #print('memory '  + str(int(win32process.GetProcessMemoryInfo(win32process.GetCurrentProcess())['WorkingSetSize'] / (1024*1024))))

    # handle the steps after flow through and then emit all of the XML and write the files
    controller.logDebug(_("Generating rendered reports in {}").format(outputFolderName))
    for cube in sortedCubeList:
        if cube.noFactsOrAllFactsSuppressed:
            for embedding in cube.embeddingList:
                Utils.embeddingGarbageCollect(embedding)
        elif cube.isEmbedded:
            continue # unless cube.noFactsOrAllFactsSuppressed we want to save it for later when we embed it
        else:
            embedding = cube.embeddingList[0]
            if not embedding.isEmbeddingOrReportBroken:
                filing.reportDriverAfterFlowThroughSuppression(embedding, xlWriter)
                filing.finishOffReportIfNotEmbedded(embedding)
            Utils.embeddingGarbageCollect(embedding)
        Utils.cubeGarbageCollect(cube)

    # now we make sure that every cube referenced by embedded command facts actually gets embedded.  this might not happen
    # if for example, the embedded command facts were all filtered out.  In that case, we make a generic embedding and
    # write it to a file, just like we would any other cube that isn't embedded anywhere by an embedding command fact.
    filing.disallowEmbeddings = True # this stops any more embeddings from happening

    for cube in filing.embeddedCubeSet:
        try:
            if cube.noFactsOrAllFactsSuppressed:
                continue
        except AttributeError: # may happen if it has been garbage collected above because cube.noFactsOrAllFactsSuppressed
            continue

        embedding = Embedding.Embedding(filing, cube, []) # make a generic embedding
        cube.embeddingList += [embedding]
        cube.isEmbedded = False
        filing.embeddingDriverBeforeFlowThroughSuppression(embedding)
        if not embedding.isEmbeddingOrReportBroken:
            # the second arg is None because we don't generate excel files for filings with embeddings.
            filing.reportDriverAfterFlowThroughSuppression(embedding, None)
            filing.finishOffReportIfNotEmbedded(embedding)

        # it might have other embeddings, but they didn't get embedded and we don't need them anymore.
        for embedding in cube.embeddingList:
            Utils.embeddingGarbageCollect(embedding)
        Utils.cubeGarbageCollect(cube)

    if len(filing.reportSummaryList) > 0:
        controller.nextFileNum = filing.reportSummaryList[-1].fileNumber + 1

    filing.unusedFactSet = set(modelXbrl.facts) - filing.usedOrBrokenFactSet

    for fact, role, cube, ignore, shortName in filing.skippedFactsList:
        if fact in filing.unusedFactSet:
            filing.strExplainSkippedFact(fact, role, shortName)

    if len(filing.unusedFactSet) > 0:
        filing.handleUncategorizedCube(xlWriter)
        controller.nextUncategorizedFileNum -= 1
        
    controller.instanceSummaryList += [Summary.InstanceSummary(filing, modelXbrl)]  
    return True




class Filing(object):
    def __init__(self, controller, modelXbrl, outputFolderName):
        self.modelXbrl = modelXbrl

        self.cubeDict = {}
        self.axisDict = {}
        self.memberDict = {}
        self.elementDict = {}
        self.factToQlabelDict = {}
        self.symbolLookupDict = {}
        self.presentationUnitToConceptDict = {}

        self.embeddedCubeSet = set()
        self.usedOrBrokenFactSet = set()
        self.unusedFactSet = set()
        self.skippedFactsList = []

        self.isRR = False
        self.hasEmbeddings = False
        self.disallowEmbeddings = True
        self.isInvestTaxonomyInDTS = False
        for namespace in self.modelXbrl.namespaceDocs:
            if re.compile('http://xbrl.(sec.gov|us)/rr/20.*').match(namespace):
                self.isRR = True
            elif re.compile('http://xbrl.sec.gov/invest/*').match(namespace):
                self.isInvestTaxonomyInDTS = True
        # These namespaces contain elements treated specially in some layouts.
        self.stmNamespace = next((n for n in self.modelXbrl.namespaceDocs.keys() if re.search('/us-gaap/20',n) is not None),None)
        self.deiNamespace = next((n for n in self.modelXbrl.namespaceDocs.keys() if re.search('/dei/20',n) is not None), None)
        self.builtinEquityColAxes = [('dei',self.deiNamespace,'LegalEntityAxis'),
                                     ('us-gaap',self.stmNamespace,'StatementEquityComponentsAxis'),
                                     ('us-gaap',self.stmNamespace,'PartnerCapitalComponentsAxis'),
                                     ('us-gaap',self.stmNamespace,'StatementClassOfStockAxis')]
        self.builtinEquityRowAxes = [('us-gaap',self.stmNamespace,'CreationDateAxis'),
                                     ('us-gaap',self.stmNamespace,'StatementScenarioAxis'),
                                     ('us-gaap',self.stmNamespace,'AdjustmentsForNewAccountingPronouncementsAxis'),
                                     ('us-gaap',self.stmNamespace,'AdjustmentsForChangeInAccountingPrincipleAxis'),
                                     ('us-gaap',self.stmNamespace,'ErrorCorrectionsAndPriorPeriodAdjustmentsRestatementByRestatementPeriodAndAmountAxis')                                     ]
        self.builtinAxisOrders = [(arelle.ModelValue.QName('us-gaap',self.stmNamespace,'StatementScenarioAxis'),
                                   ['ScenarioPreviouslyReportedMember',
                                    'RestatementAdjustmentMember',
                                    'ChangeInAccountingPrincipleMember'],
                                   ['ScenarioUnspecifiedDomain'])]
        self.builtinLineItems = [arelle.ModelValue.QName('us-gaap',self.stmNamespace,'StatementLineItems')]
        self.segmentHeadingStopList = [arelle.ModelValue.QName(x,y,z) for x,y,z in self.builtinEquityRowAxes]
        # TODO: change flags like isRR, isInvest to contain the actual namespace or None.
        self.factToEmbeddingDict = {}
        self.factFootnoteDict = defaultdict(list)
        self.startEndContextDict = {}

        self.numReports = 0

        self.controller = controller
        self.reportXmlFormat = 'xml' in controller.reportFormat.casefold()
        self.reportHtmlFormat = 'html' in controller.reportFormat.casefold()
        self.fileNamePrefix = 'R'
        self.fileNameBase = os.path.normpath(os.path.join(os.path.dirname(controller.webCache.normalizeUrl(modelXbrl.fileSource.url)) ,outputFolderName))
        if not os.path.exists(self.fileNameBase):  # This is usually the Reports subfolder.
            os.mkdir(self.fileNameBase)

        if controller.reportXslt:
            self.transform = lxml.etree.XSLT(lxml.etree.parse(controller.reportXslt))
        if controller.summaryXslt:
            self.summary_transform = lxml.etree.XSLT(lxml.etree.parse(controller.summaryXslt))
        self.reportSummaryList = []

        self.rowSeparatorStr = ' | '
        self.titleSeparatorStr = ' - '
        self.verboseHeadingsForDebugging = False
        self.ignoredPreferredLabels = [] # locations where the preferred label role was incompatible with the concept type.
        self.entrypoint = modelXbrl.modelDocument.basename

    def __str__(self):
        return "[Filing {!s}]".format(self.entrypoint)



    def populateAndLinkClasses(self, uncategorizedCube = None):
        duplicateFacts = set()

        if uncategorizedCube is not None:
            for fact in self.unusedFactSet:
                # we know these facts aren't broken, because broken facts weren't added to self.unusedFactSet.
                try:
                    element = self.elementDict[fact.qname]
                except KeyError:
                    element = Element(fact.concept)
                    self.elementDict[fact.qname] = element
                    element.linkCube(uncategorizedCube)
            uncategorizedCube.presentationGroup = PresentationGroup.PresentationGroup(self, uncategorizedCube)
            facts = self.unusedFactSet

        else:
            # build cubes
            for linkroleUri in self.modelXbrl.relationshipSet(arelle.XbrlConst.parentChild).linkRoleUris:
                cube = Cube.Cube(self, linkroleUri)
                self.cubeDict[linkroleUri] = cube
                cube.presentationGroup = PresentationGroup.PresentationGroup(self, cube)

            # handle axes across all cubes where defaults are missing in the definition or presentation linkbases
            # presentation linkbase
            parentChildRelationshipSet = self.modelXbrl.relationshipSet(arelle.XbrlConst.parentChild)
            parentChildRelationshipSet.loadModelRelationshipsTo()
            parentChildRelationshipSet.loadModelRelationshipsFrom()
            # Find the axes in presentation groups
            toDimensions = {c for c in parentChildRelationshipSet.modelRelationshipsTo.keys() if c.isDimensionItem}
            fromDimensions = {c for c in parentChildRelationshipSet.modelRelationshipsFrom.keys() if c.isDimensionItem}
            # definition linkbase
            dimensionDefaultRelationshipSet = self.modelXbrl.relationshipSet(arelle.XbrlConst.dimensionDefault)
            dimensionDefaultRelationshipSet.loadModelRelationshipsFrom()
            for concept in set.union(fromDimensions,toDimensions):
                defaultSet = {ddrel.toModelObject for ddrel in dimensionDefaultRelationshipSet.modelRelationshipsFrom[concept]}
                for linkroleUri in {pcrel.linkrole for pcrel in parentChildRelationshipSet.modelRelationshipsFrom[concept]}:
                    # although valid XBRL has at most one default, we don't assume it; instead we act like it's a set of defaults.
                    # check to see whether the defaults are all children of the axis in this presentation group.
                    defaultChildSet = {pcrel.toModelObject 
                                       for pcrel in Utils.modelRelationshipsTransitiveFrom(parentChildRelationshipSet, concept, linkroleUri)
                                       if pcrel.toModelObject in defaultSet}
                    if (len(defaultSet)==0  # axis had no default at all
                            or defaultSet != defaultChildSet):
                        cube = self.cubeDict[linkroleUri]
                        cube.defaultFilteredOutAxisSet.add(concept.qname)             

            # print warnings of missing defaults for each cube
            for cube in self.cubeDict.values():
                if len(cube.defaultFilteredOutAxisSet) > 0:
                    self.controller.logDebug("In ''{}'', the children of axes {} do not include their defaults."
                                     .format(cube.shortName, cube.defaultFilteredOutAxisSet))


            # initialize elements
            for qname, factSet in self.modelXbrl.factsByQname.items():

                # we are looking to see if we have "duplicate" facts.  a duplicate fact is one with the same qname, context and unit
                # as another fact.  in this case, we keep the first fact with an 'en-US' language, or if there is none, keep the first fact.
                # the others need to be proactively added to the set of unused facts.
                if len(factSet) > 1:
                    sortedFactList = sorted(factSet, key = lambda thing : (thing.contextID, thing.unitID, thing.sourceline))
                    while len(sortedFactList) > 0:

                        firstFact = sortedFactList.pop(0)
                        if firstFact.xmlLang == 'en-US':
                            firstEnUsLangFact = firstFact
                        else:
                            firstEnUsLangFact = None

                        discardedLineNumberList = []
                        counter = 0

                        # finds facts with same qname, context and unit as firstFact
                        while (len(sortedFactList) > 0 and
                               sortedFactList[0].qname == firstFact.qname and
                               sortedFactList[0].context == firstFact.context and
                               sortedFactList[0].unitID == firstFact.unitID):
                            counter += 1
                            fact = sortedFactList.pop(0)
                            if firstEnUsLangFact is None and fact.xmlLang == 'en-US':
                                firstEnUsLangFact = fact # keeping this fact
                            else:
                                duplicateFacts.add(fact) # not keeping this fact
                                discardedLineNumberList += [str(fact.sourceline)] # these are added in sorted order by sourceline

                        if counter > 0:
                            if firstEnUsLangFact is None:
                                lineNumOfFactWeAreKeeping = firstFact.sourceline # there is no en-US fact, keep the first fact we found
                            else:
                                lineNumOfFactWeAreKeeping = firstEnUsLangFact.sourceline # we did find an en-US fact, keep it.
                                if firstFact != firstEnUsLangFact:
                                    duplicateFacts.add(firstFact)
                                    discardedLineNumberList = [str(firstFact.sourceline)] + discardedLineNumberList # prepend to maintain sorted order

                            # start it off because we can assume that these facts have a qname and a context
                            # note that even if we are keeping firstEnUsLangFact, we are only printing qname, contextID and unitID,
                            # which are the same for both firstFact and firstEnUsLangFact.
                            qnameContextIDUnitStr = 'qname {!s}, context {}'.format(firstFact.qname, firstFact.contextID)
                            if firstFact.unit is not None:
                                qnameContextIDUnitStr += ', unit ' + firstFact.unitID
                            #message = ErrorMgr.getError('DUPLICATE_FACT_SUPPRESSION').format(qnameContextIDUnitStr, lineNumOfFactWeAreKeeping, ', '.join(discardedLineNumberList))
                            self.controller.logWarn("There are multiple facts with {}. The fact on line {} of the instance " \
                                                    "document will be rendered, and the rest at line(s) {} will not.".format(
                                                    qnameContextIDUnitStr, lineNumOfFactWeAreKeeping,
                                                    ', '.join(discardedLineNumberList)))

                for fact in factSet: # we only want one thing, but we don't want to pop from the set so we "loop" and then break right away

                    elementBroken = False

                    if fact.concept is None:
                        #conceptErrStr = ErrorMgr.getError('FACT_DECLARATION_BROKEN').format(qname)
                        self.controller.logWarn("The element declaration for {}, or one of its facts, is broken. They will all be " \
                                                "ignored.".format(qname))
                        elementBroken = True

                    elif fact.concept.type is None:
                        #typeErrStr = ErrorMgr.getError('The Type declaration for Element {} is either broken or missing. The Element will be ignored.').format(qname)
                        self.controller.logWarn("The Type declaration for Element {} is either broken or missing. The " \
                                                "Element will be ignored.".format(qname))
                        elementBroken = True

                    if fact.context is None or elementBroken: # we will print the error if firstContext is broken later
                        continue # see if there are other facts for this concept with good contexts before we break from the loop, we still might make the Element yet

                    self.elementDict[qname] = Element(fact.concept)
                    break # we don't need to look at more facts from the fact set, we're just trying to make elements.

            # build presentation groups
            for concept in self.modelXbrl.qnameConcepts.values():
                for relationship in self.modelXbrl.relationshipSet(arelle.XbrlConst.parentChild).toModelObject(concept):
                    cube = self.cubeDict[relationship.linkrole]
                    cube.presentationGroup.traverseToRootOrRoots(concept, None, None, None, set())
                    try:
                        element = self.elementDict[concept.qname] # retrieve active Element
                        element.linkCube(cube) # link element to this cube.
                    except KeyError:
                        pass

            # footnotes
            for relationship in self.modelXbrl.relationshipSet('XBRL-footnotes').modelRelationships:
                # relationship.fromModelObject is a fact
                # relationship.toModelObject is a resource
                # make sure Element is active and that no Error is caught by relationshipErrorThrower()
                # if relationship.fromModelObject.qname in self.elementDict and not self.relationshipErrorThrower(relationship, 'Footnote'):
                #if relationship.fromModelObject.qname in self.elementDict:
                self.factFootnoteDict[relationship.fromModelObject].append((relationship.toModelObject, relationship.toModelObject.viewText()))

            facts = self.modelXbrl.facts

        for fact in facts:
            if fact.isTuple:
                #tupleErrStr = ErrorMgr.getError('UNSUPPORTED_TUPLE_FOUND').format(fact.qname)
                self.controller.logWarn("A Fact with Qname {} is a Tuple and Tuples are forbidden by the EDGAR Filer " \
                                        "Manual. The Fact will be ignored.".format(fact.qname))
                self.usedOrBrokenFactSet.add(fact) #now bad fact won't come back to bite us when processing isUncategorizedFacts
                continue

            if fact.context is None:
                #contextErrStr1 = ErrorMgr.getError('CONTEXT_BROKEN').format(fact.qname, fact.value)
                self.controller.logWarn("Either the Context of a Fact with Qname {}, or the reference to the Context " \
                                        "in the Fact is broken. The Fact will be ignored. The value of this Fact " \
                                        "is {}.".format(fact.qname, fact.value))
                self.usedOrBrokenFactSet.add(fact) #now bad fact won't come back to bite us when processing isUncategorizedFacts
                continue

            if fact.context.scenario is not None:
                #scenarioErrStr = ErrorMgr.getError('IMPROPER_CONTEXT_FOUND').format(fact.context.id)
                self.controller.logWarn("The Context {} has a scenario attribute. Such attributes are forbidden by " \
                                        "the EDGAR Filer Manual. This filing will not validate, but this should not " \
                                        "interfere with rendering".format(fact.contextID))
            try:
                element = self.elementDict[fact.qname]
            except KeyError:
                self.usedOrBrokenFactSet.add(fact) #now bad fact won't come back to bite us when processing isUncategorizedFacts
                continue # fact was rejected in first loop of this function because of problem with the Element

            # this is after we check for the bad stuff so that we make sure not to put those into usedOrBrokenFactSet
            # so that they don't break when processing isUncategorizedFacts
            if fact in duplicateFacts:
                continue

            # first see if fact's value is an embedded command, then check if it's a qlabel fact.
            if not fact.isNumeric:
                if fact.concept.isTextBlock:
                    isEmbeddedCommand = self.checkForEmbeddedCommandAndProcessIt(fact)
                else:
                    isEmbeddedCommand = False

                if not isEmbeddedCommand and re.compile('[a-zA-Z-]+:[a-zA-Z]+').match(fact.value):
                    try:
                        prefix, ignore, localName = fact.value.partition(':')
                        namespaceURI = self.modelXbrl.prefixedNamespaces[prefix]
                        qname = arelle.ModelValue.QName(prefix, namespaceURI, localName)
                        if qname in self.modelXbrl.qnameConcepts:
                            self.factToQlabelDict[fact] = qname
                    except KeyError:
                        pass

            axisMemberLookupDict = {}

            # add period and unit to axisMemberLookupDict
            startEndContext = None
            con = fact.context
            if fact.context is not None:
                if con.instantDatetime is not None: # is an instant
                    startEndTuple = (None, con.instantDatetime)
                else: # is a startEndContext
                    startEndTuple = (con.startDatetime, con.endDatetime)
                try:
                    startEndContext = self.startEndContextDict[startEndTuple]
                except KeyError:
                    startEndContext = StartEndContext(con, startEndTuple)
                    self.startEndContextDict[startEndTuple] = startEndContext
                axisMemberLookupDict['period'] = startEndContext

            if fact.unit is not None:
                axisMemberLookupDict['unit'] = fact.unit.id

            # add each axis to axisMemberLookupDict
            for arelleDimension in fact.context.qnameDims.values():
                dimensionConcept = arelleDimension.dimension
                memberConcept = arelleDimension.member
                if dimensionConcept is None:
                    #errStr1 = ErrorMgr.getError('XBRL_DIMENSIONS_INVALID_AXIS_BROKEN').format(fact.context.id, fact.qname)
                    self.controller.logWarn("One of the Axes referenced by the Context {} of Fact {}, or the reference " \
                                            "itself, is broken. The Axis will be ignored for this Fact.".format(
                                            fact.contextID, fact.qname))

                elif memberConcept is None:
                    #errStr2 = ErrorMgr.getError('XBRL_DIMENSIONS_INVALID_AXIS_MEMBER_BROKEN').format(dimensionConcept.qname, fact.qname, fact.context.id)
                    self.controller.logWarn("The Member of Axis {} is broken as referenced by the Fact {} with Context {}. " \
                                            "The Axis and Member will be ignored for this Fact.".format(dimensionConcept.qname,
                                            fact.qname, fact.contextID))

                else:
                    try:
                        axis = self.axisDict[dimensionConcept.qname]
                    except KeyError:
                        axis = Axis(dimensionConcept)
                        for relationship in self.modelXbrl.relationshipSet(arelle.XbrlConst.dimensionDefault).fromModelObject(dimensionConcept):
                            axis.defaultArelleConcept = relationship.toModelObject
                            break
                        self.axisDict[dimensionConcept.qname] = axis
                    if arelleDimension.isExplicit: # if true, Member exists, else None. there's also isTyped, for typed dims.
                        try:
                            member = self.memberDict[memberConcept.qname]
                        except KeyError:
                            member = Member(memberConcept)
                            self.memberDict[memberConcept.qname] = member
                        member.linkAxis(axis)
                        axis.linkMember(member)
                    axisMemberLookupDict[axis.arelleConcept.qname] = member.arelleConcept.qname

                    # while we're at it, do some other stuff
                    for cube in element.inCubes.values():
                        cube.hasAxes[axis.arelleConcept.qname] = axis
                        cube.hasMembers[member.arelleConcept.qname] = member
                        axis.linkCube(cube)

            for cube in element.inCubes.values():
                # the None in the tuple is only to handle periodStartLabels and periodEndLabels later on
                cube.factMemberships += [(fact, axisMemberLookupDict, None)]
                cube.hasElements.add(fact.concept)
                if fact.unit is not None:
                    cube.unitAxis[fact.unit.id] = fact.unit
                if startEndContext is not None:
                    cube.timeAxis.add(startEndContext)




    def checkForEmbeddedCommandAndProcessIt(self, fact):
        # partition('~') on a string breaks up a string into a tuple with before the first ~, the ~, and then after the ~. 
        ignore, tilde, rightOfTilde = fact.value.partition('~')
        if tilde == '':
            return False
        leftOfTilde, tilde, ignore = rightOfTilde.partition('~')
        if tilde == '' or leftOfTilde == '':
            return False
        commandText = leftOfTilde

        # we take out the URI first, because it might have double quotes in it and we want to cleanse the rest of the
        # command of double quotes since the separator command wraps the separator character in double quotes.
        commandTextList = commandText.split(maxsplit=1) # this is a list of length 1 or 2.
        linkroleUri = commandTextList.pop(0) # now commandTextList is a list of length 0 or 1
        try:
            cube = self.cubeDict[linkroleUri]
        except KeyError:
            return False # not a valid linkroleUri
        if len(commandTextList) > 0:
            commandTextList = commandTextList[0].replace('"', ' ')
            commandTextList = commandTextList.split()

        outputList = []
        tokenCounter = 1
        while len(commandTextList) > 0:
            listToAddToOutput = []

            token0 = commandTextList.pop(0)
            tokenCounter += 1
            token0Lower = token0.casefold()
            if token0Lower in {'row', 'column'}:
                listToAddToOutput += [token0Lower]
            else:
                errorStr = Utils.printErrorStringToDiscribeEmbeddedTextBlockFact(fact)
                #message = ErrorMgr.getError('EMBEDDED_COMMAND_TOKEN_NOT_ROW_OR_COLUMN_ERROR').format(token0, tokenCounter, errorStr)
                self.controller.logError("The token {}, at position{} in the list of tokens in {}, is malformed. " \
                                         "An individual command can only start with row or column. These embedded " \
                                         "commands will not be rendered.".format(token0, tokenCounter, errorStr))
                return False


            token1 = commandTextList.pop(0)
            tokenCounter += 1
            token1Lower = token1.casefold()
            if token1Lower in {'period', 'unit', 'primary'}:
                listToAddToOutput += [token1Lower]
            elif '_' in token1:
                listToAddToOutput += [arelle.ModelValue.qname(fact, token1.replace('_',':',1))] # only replace first _, because qnames can have _

            # separator is not supported, we just pop it off and ignore it
            elif token1Lower == 'separator':
                if commandTextList.pop(0).casefold() == 'segment':
                    commandTextList.pop(0)
                    tokenCounter += 1
                tokenCounter += 1
                errorStr = Utils.printErrorStringToDiscribeEmbeddedTextBlockFact(fact)
                #message = ErrorMgr.getError('EMBEDDED_COMMAND_SEPARATOR_USED_WARNING').format(token1, tokenCounter, errorStr)
                self.controller.logInfo("The token at position {} in the list of tokens in {}, is separator. " \
                                        "Currently, this keyword is not supported and was ignored.".format(
                                        tokenCounter, errorStr))
                continue

            else:
                errorStr = Utils.printErrorStringToDiscribeEmbeddedTextBlockFact(fact)
                #message = ErrorMgr.getError('EMBEDDED_COMMAND_INVALID_FIRST_TOKEN_ERROR').format(token1, tokenCounter, errorStr)
                self.controller.logError("The token at position {} in the list of tokens in {}, is malformed. " \
                                         "The axis name can only be period, unit, primary or have an underscore. " \
                                         "These embedded commands will not be rendered.".format(tokenCounter, errorStr))
                return False

            token2 = commandTextList.pop(0)
            tokenCounter += 1
            token2Lower = token2.casefold()
            if token2Lower in {'compact', 'nodisplay'}:
                listToAddToOutput += [token2Lower]
            elif token2Lower == 'grouped':
                listToAddToOutput += ['compact']
                errorStr = Utils.printErrorStringToDiscribeEmbeddedTextBlockFact(fact)
                #message = ErrorMgr.getError('EMBEDDED_COMMAND_GROUPED_USED_WARNING').format(token2, tokenCounter, errorStr)
                self.controller.logInfo("The token at position {} in the list of tokens in {}, is grouped. " \
                                        "Currently, this keyword is not supported and was replaced with compact.".format(
                                        tokenCounter, errorStr))
            elif token2Lower == 'unitcell':
                listToAddToOutput += ['compact']
                errorStr = Utils.printErrorStringToDiscribeEmbeddedTextBlockFact(fact)
                #message = ErrorMgr.getError('EMBEDDED_COMMAND_UNITCELL_USED_WARNING').format(token2, tokenCounter, errorStr)
                self.controller.logInfo("The token at position {} in the list of tokens in {}, is unitcell. " \
                                        "Currently, this keyword is not supported and was replaced with compact.".format(
                                        tokenCounter, errorStr))
            else:
                errorStr = Utils.printErrorStringToDiscribeEmbeddedTextBlockFact(fact)
                #message = ErrorMgr.getError('EMBEDDED_COMMAND_INVALID_SECOND_TOKEN_ERROR').format(token2, tokenCounter, errorStr)
                self.controller.logError("The token {}, at position {} in the list of tokens in {}, is malformed. The second token " \
                                         "of an embedded command can only be compact, grouped, nodisplay or unitcell. These " \
                                         "embedded commands will not be rendered.".format(token2, tokenCounter, errorStr))
                return False

            # there could be multiple members, so grab them all here
            tempList = []
            while len(commandTextList) > 0 and commandTextList[0].casefold() not in {'row', 'column'}:
                tempList += [commandTextList.pop(0)]

            for tokenMember in tempList: 
                tokenCounter += 1
                if '_' in tokenMember:
                    listToAddToOutput += [arelle.ModelValue.qname(fact, tokenMember.replace('_',':',1))]
                elif tokenMember == '*' and len(tempList) == 1:
                    listToAddToOutput += [tokenMember]
                else:
                    errorStr = Utils.printErrorStringToDiscribeEmbeddedTextBlockFact(fact)
                    #message = ErrorMgr.getError('EMBEDDED_COMMAND_INVALID_MEMBER_NAME_ERROR').format(tokenMember, tokenCounter, errorStr)
                    self.controller.logError("The token {}, at position {} in the list of tokens in {}, is malformed. " \
                                             "The member name must either be * or have an underscore, and if there is " \
                                             "a list of members for this axis, they all must contain an underscore. " \
                                             "These embedded commands will not be rendered.".format(
                                             tokenMember, tokenCounter, errorStr))
                    return False

            outputList += [listToAddToOutput]

        cube.isEmbedded = True
        self.hasEmbeddings = True
        self.disallowEmbeddings = False
        
        embedding = Embedding.Embedding(self, cube, outputList, factThatContainsEmbeddedCommand = fact)
        cube.embeddingList += [embedding]
        self.factToEmbeddingDict[fact] = embedding
        return True





    def handleUncategorizedCube(self, xlWriter):
        # get a fresh start, kill all the old data structures that get built in populateAndLinkClasses()
        for element in self.elementDict.values():
            element.__dict__.clear()
            del element
        self.elementDict = {}

        for cube in self.cubeDict.values():
            cube.__dict__.clear()
            del cube
        self.cubeDict = {}

        for member in self.memberDict.values():
            member.__dict__.clear()
            del member
        self.memberDict = {}

        for axis in self.axisDict.values():
            axis.__dict__.clear()
            del axis
        self.axisDict = {}

        for startEndContext in self.startEndContextDict.values():
            startEndContext.__dict__.clear()
            del startEndContext
        self.startEndContextDict = {}

        uncategorizedCube = Cube.Cube(self, 'http://xbrl.sec.gov/role/uncategorizedFacts')
        uncategorizedCube.fileNumber = self.controller.nextUncategorizedFileNum
        uncategorizedCube.shortName = uncategorizedCube.definitionText = 'Uncategorized Items - ' + self.entrypoint
        uncategorizedCube.isElements = True
        
        # now run populateAndLinkClasses() again and let it re-populate and re-link everything from scratch but let it do so
        # only with filing.unusedFactSet as it's fact set and with only the uncategorizedCube, and no other cubes.
        self.cubeDict[uncategorizedCube.linkroleUri] = uncategorizedCube
        self.populateAndLinkClasses(uncategorizedCube = uncategorizedCube)

        self.cubeDriverBeforeFlowThroughSuppression(uncategorizedCube)
        embedding = Embedding.Embedding(self, uncategorizedCube, [])
        uncategorizedCube.embeddingList = [embedding]
        self.embeddingDriverBeforeFlowThroughSuppression(embedding)
        self.reportDriverAfterFlowThroughSuppression(embedding, xlWriter)
        self.finishOffReportIfNotEmbedded(embedding)
        Utils.embeddingGarbageCollect(embedding)
        Utils.cubeGarbageCollect(uncategorizedCube)




    def cubeDriverBeforeFlowThroughSuppression(self, cube):
        if cube.isUncategorizedFacts:
            cube.presentationGroup.generateUncategorizedFactsPresentationGroup()
        else:
            if len(cube.hasElements) == 0 or len(cube.factMemberships) == 0:
                cube.noFactsOrAllFactsSuppressed = True
                return
            cube.presentationGroup.startPreorderTraversal()
            if cube.noFactsOrAllFactsSuppressed:
                return
            cube.areTherePhantomAxesInPGWithNoDefault()
            if cube.noFactsOrAllFactsSuppressed:
                return

            cube.checkForTransposedUnlabeledAndElements()
            if len(cube.periodStartEndLabelDict) > 0:
                cube.handlePeriodStartEndLabel() # needs preferred labels from the presentationGroup

        cube.populateUnitPseudoaxis()
        cube.populatePeriodPseudoaxis()

        if self.controller.debugMode:
            cube.printCube()


    def embeddingDriverBeforeFlowThroughSuppression(self, embedding):
        cube = embedding.cube

        embedding.generateStandardEmbeddedCommandsFromPresentationGroup()
        if cube.isTransposed:
            embedding.handleTransposedByModifyingCommandText()
        embedding.buildAndProcessCommands()
        if embedding.isEmbeddingOrReportBroken:
            return

        embedding.processOrFilterFacts()
        if embedding.isEmbeddingOrReportBroken:
            return

        embedding.possiblyReorderUnitsAfterTheFactAccordingToPresentationGroup()

        if self.controller.debugMode:
            embedding.printEmbedding()

        report = embedding.report = Report.Report(self, cube, embedding)
        report.generateRowsOrCols('col', sorted(embedding.factAxisMemberGroupList, key=lambda thing: thing.axisMemberPositionTupleColList))

        # this is because if the {Elements} view is used, then you might have lots of facts right next to each other with the same qname
        # this is fine, but each time you render, they might appear in a different order.  so this will sort the facts by value
        # so that each run the same facts don't appear in different orders.
        if cube.isElements:
            sortedFAMGL = sorted(embedding.factAxisMemberGroupList, key=lambda thing: (thing.axisMemberPositionTupleRowList, thing.fact.value))
        else:
            sortedFAMGL = sorted(embedding.factAxisMemberGroupList, key=lambda thing: thing.axisMemberPositionTupleRowList)
        report.generateRowsOrCols('row', sortedFAMGL)

        if not cube.isElements:
            if self.hasEmbeddings:
                report.decideWhetherToRepressPeriodHeadings()
            if not cube.isUnlabeled:
                report.promoteAxes()

            if embedding.rowUnitPosition != -1:
                report.mergeRowsOrColsIfUnitsCompatible('row', report.rowList)
            elif embedding.columnUnitPosition != -1:
                report.mergeRowsOrColsIfUnitsCompatible('col', report.colList)

            if embedding.rowPeriodPosition != -1:
                report.mergeRowsOrColsInstantsIntoDurationsIfUnitsCompatible('row', report.rowList)
            elif embedding.columnPeriodPosition != -1:
                report.mergeRowsOrColsInstantsIntoDurationsIfUnitsCompatible('col', report.colList)

            report.hideRedundantColumns()


    def reportDriverAfterFlowThroughSuppression(self, embedding, xlWriter):
        report = embedding.report
        cube = embedding.cube

        if embedding.rowPeriodPosition != -1:
            report.HideAdjacentInstantRows()
        elif cube.isStatementOfCashFlows:
            self.RemoveStuntedCashFlowColumns(report,cube)

        report.scaleUnitGlobally()

        if      (len(self.factFootnoteDict) > 0 and
                 not {factAxisMemberGroup.fact for factAxisMemberGroup in embedding.factAxisMemberGroupList}.isdisjoint(self.factFootnoteDict)):
            report.handleFootnotes()
            report.setAndMergeFootnoteRowsAndColumns('row', report.rowList)
            report.setAndMergeFootnoteRowsAndColumns('col', report.colList)

        report.removeVerticalInteriorSymbols()

        #if len(embedding.groupedAxisQnameSet) > 0:
        #    report.handleGrouped()

        if cube.isElements or not cube.isEmbedded:
            if      (embedding.columnPeriodPosition != -1 or
                     {command.pseudoAxis for command in embedding.rowCommands}.isdisjoint(self.segmentHeadingStopList)):
                report.makeSegmentTitleRows()

            # if embedding.rowPrimaryPosition != -1, then primary elements aren't on the rows, so no abstracts to add
            if embedding.rowPrimaryPosition != -1 and not cube.isUnlabeled:
                report.addAbstracts()

        if cube.isElements:
            report.generateRowAndOrColHeadingsForElements()
        else:
            report.generateRowAndOrColHeadingsGeneralCase()

        report.emitRFile()

        if xlWriter:
            # we pass the cube's shortname since it doesn't have units and stuff tacked onto the end.
            xlWriter.createWorkSheet(cube.fileNumber, cube.shortName)
            xlWriter.buildWorkSheet(report)


    def finishOffReportIfNotEmbedded(self, embedding):
        reportSummary = ReportSummary()
        embedding.report.createReportSummary(reportSummary)
        embedding.report.writeHtmlAndOrXmlFiles(reportSummary)
        self.reportSummaryList += [reportSummary]






    def RemoveStuntedCashFlowColumns(self,report,cube):
        visibleColumns = [col for col in report.colList if not col.isHidden]
        didWeHideAnyCols = False
        if len(visibleColumns)>0:
            remainingVisibleColumns = visibleColumns.copy()
            maxMonths = max(col.startEndContext.numMonths for col in visibleColumns)
            minFacts = min(len(col.factList) for col in visibleColumns if col.startEndContext.numMonths==maxMonths)
            minToKeep = math.floor(.25*minFacts)
            for col in visibleColumns:
                if col.startEndContext.numMonths < maxMonths and len(col.factList) < minToKeep:
                    self.controller.logInfo(("Columns in cash flow ''{}'' have maximum duration {} months and at least {} " \
                                  "values. Shorter duration columns must have at least one fourth ({}) as many values. " \
                                  "Column '{}' is shorter ({} months) and has only {} values, so it is being removed.")
                                  .format(cube.shortName, maxMonths, minFacts, minToKeep, col.startEndContext,
                                  col.startEndContext.numMonths,len(col.factList)))
                    col.isHidden = True
                    didWeHideAnyCols = True
                    remainingVisibleColumns.remove(col)
                    for fact in col.factList: 
                        appearsInOtherColumn = False
                        for otherCol in remainingVisibleColumns:
                            if fact in otherCol.factList:
                                appearsInOtherColumn = True
                                break
                        if not appearsInOtherColumn:
                            cube.factMemberships = [fm for fm in cube.factMemberships if not fact is fm[0]] # local removal
                            # Go look whether the fact is now completely uncategorized
                            if fact in self.usedOrBrokenFactSet:
                                element = self.elementDict[fact.elementQname]
                                appearsInOtherCube = False
                                for c in element.inCubes.values():
                                    if hasattr(c,'factMemberships'): # Some Cube objects seem uninitialized, not sure why.
                                        for fm in c.factMemberships:
                                            if fm[0] is fact: # Assumes that factMemberships is an accurate list of facts presented.
                                                appearsInOtherCube = True
                                                break # Only need to find one other place the fact appears
                                        if appearsInOtherCube is True:
                                            break
                                if appearsInOtherCube is False:
                                    # This was the only place the fact was presented, and now it's hidden.
                                    self.usedOrBrokenFactSet.remove(fact)             

        if didWeHideAnyCols:
            Utils.hideEmptyRows(report.rowList)






    # this is only for financial reports, so we know that there are no embedded commands.  this means that for each cube,
    # there is only one embedding object, but we have to get that this way: cube.embeddingList[0].  also, always, in general
    # each embedding only ever has one and only one report.  so for financial reports, report is cube.embeddingList[0].report.
    def filterOutColumnsWhereAllElementsAreInOtherReports(self, sortedCubeList):
        # we separate nonStatementElementsAndElementMemberPairs from all of the cubes because we can do this just once and not run through them each time
        statementCubesList = []
        nonStatementElementsAndElementMemberPairs = set()
        for cube in sortedCubeList:
            if not cube.noFactsOrAllFactsSuppressed and len(cube.embeddingList) == 1 and not cube.embeddingList[0].isEmbeddingOrReportBroken:
                if cube.cubeType == 'statement' and not cube.isStatementOfEquity:
                    statementCubesList += [cube]
                else:
                    nonStatementElementsAndElementMemberPairs.update(cube.embeddingList[0].hasElementsAndElementMemberPairs)

        for i, cube in enumerate(statementCubesList):
            # initialize to all the non statement elements and non statement element member pairs
            elementQnamesInOtherReportsAndElementQnameMemberPairs = nonStatementElementsAndElementMemberPairs.copy()
            # now add other statements
            for j, otherStatement in enumerate(statementCubesList):
                if i != j:
                    elementQnamesInOtherReportsAndElementQnameMemberPairs.update(otherStatement.embeddingList[0].hasElementsAndElementMemberPairs)
            # now elementQnamesInOtherReportsAndElementQnameMemberPairs has all the qnames used in every other report except for the statement we're using

            report = cube.embeddingList[0].report # we know there's no embeddings, so the report is on the first and only embedding
            columnsToKill = []
            nonHiddenColCount = 0

            elementQnamesThatWillBeKeptProvidingThatWeHideTheseCols = set() # if we kill a few cols, we will want to update embedding.hasElements
            for col in report.colList:

                if not col.isHidden:
                    nonHiddenColCount += 1
                    setOfElementQnamesInCol = {fact.qname for fact in col.factList}
                    setOfElementQnamesAndQnameMemberPairsForCol = setOfElementQnamesInCol.union(col.elementQnameMemberForColHidingSet)
                    # the operator <= means subset, not a proper subset.  so if all the facts in the column are elsewhere, then hide column.
                    if setOfElementQnamesAndQnameMemberPairsForCol <= elementQnamesInOtherReportsAndElementQnameMemberPairs:
                        columnsToKill += [col]
                    else:
                        elementQnamesThatWillBeKeptProvidingThatWeHideTheseCols.update(setOfElementQnamesInCol)

            if 0 < len(columnsToKill) < nonHiddenColCount:
                cube.embeddingList[0].hasElements = elementQnamesThatWillBeKeptProvidingThatWeHideTheseCols # update hasElements, might have less now
                for col in columnsToKill:
                    col.isHidden = True

                self.controller.logInfo("In ''{}'', column(s) {!s} are contained in other reports, so were removed by flow through suppression.".format(
                                            cube.shortName, ', '.join([str(col.index + 1) for col in columnsToKill])))
                Utils.hideEmptyRows(report.rowList)


    def strExplainSkippedFact(self, fact, role, shortName):
        # we skipped over this fact because it could not be placed
        # Produce a string explaining for this instant fact why it could not be presented 
        # with a periodStart or periodEnd label in this presentation group.
        qname = fact.qname
        value = Utils.strFactValue(fact, preferredLabel=role)
        endTime = fact.context.period.stringValue.strip()
        word = 'Starting or Ending'
        if role is not None:
            role = role.rsplit('/')[-1]
            if 'Start' in role:
                word = 'starting'
            elif 'End' in role:
                word = 'ending'
        #message = ErrorMgr.getError('SKIPPED_FACT_WARNING').format(shortName,qname,value,role,word,endTime)
        self.controller.logWarn("In ''{}'', fact {} with value {} and preferred label {}, was not shown because there are " \
                                "no facts in a duration {} at {}. Change the preferred label role or add facts.".format(
                                shortName, qname, value, role, word, endTime))





class ReportSummary(object):
    def __init__(self):
        self.order = None
        self.isDefault = None
        self.hasEmbeddedReports = None
        self.longName = None
        self.shortName = None
        self.role = None
        self.logList = None
        self.xmlFileName = None
        self.htmlFileName = None
        self.isUncategorized = False

class StartEndContext(object):
    def __init__(self, context, startEndTuple):
        # be really careful when using this context.  many contexts from the instance can share this object,
        # probably not the right one.
        self.context = context
        self.startTime = startEndTuple[0]
        self.endTime = startEndTuple[1]
        self.label = (self.endTime - datetime.timedelta(days=1)).strftime('%b. %d, %Y')

        if self.startTime == None:
            self.periodTypeStr = 'instant'
            self.startTimePretty = None
            self.numMonths = 0
        else:
            self.periodTypeStr = 'duration'
            self.startTimePretty = (self.startTime).strftime('%Y-%m-%dT%H:%M:%S')
            self.numMonths = self.startEndContextInMonths()
        self.endTimePretty = (self.endTime - datetime.timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S')

    def startEndContextInMonths(self):
        modifiedEndTime = self.endTime + datetime.timedelta(days=15) # we add to it because it rounds down
        delta = dateutil.relativedelta.relativedelta(modifiedEndTime, self.startTime)
        return delta.years * 12 + delta.months
    
    def startOrInstantTime(self):
        if self.startTime is None:
            return self.endTime
        return self.startTime
    
    def __str__(self):
        if self.periodTypeStr=='instant':
            return "[{}]".format(self.endTimePretty[:10])
        else:
            return "[{} {}m {}]".format(self.startTimePretty[:10],self.numMonths,self.endTimePretty[:10])

class Axis(object):
    def __init__(self, arelleConcept):
        self.inCubes = {}
        self.hasMembers = {}
        self.arelleConcept = arelleConcept
        self.defaultArelleConcept = None
    def linkCube(self, cubeObj):
        self.inCubes[cubeObj.linkroleUri] = cubeObj
    def linkMember(self, memObj):
        self.hasMembers[memObj.arelleConcept.qname] = memObj
    def __repr__(self):
        return "axis(arelleConcept={}, default={})".format(self.arelleConcept, self.defaultArelleConcept)

class Member(object):
    def __init__(self, arelleConcept):
        self.hasMembers = {}
        self.arelleConcept = arelleConcept
        self.axis = None
        self.parent = None
    def linkAxis(self, axisObj):
        self.axis = axisObj
    def linkParent(self, parentObj):
        self.parent = parentObj

class Element(object):
    def __init__(self, arelleConcept):
        self.inCubes = {}
        self.arelleConcept = arelleConcept
    def linkCube(self, cube):
        self.inCubes[cube.linkroleUri] = cube